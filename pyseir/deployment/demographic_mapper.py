import logging
import pandas as pd
import numpy as np
from collections import defaultdict
from enum import Enum
from datetime import datetime, timedelta


class CovidMeasure(Enum):
    """
    - hospitalization_infected: probability an infected person is hospitalized,
                                including non-ICU and ICU admission.
    - hospitalization_icu_infected: probability an infected person is
                                    admitted to ICU.
    - hospitalization_general_infected: probability an infected person is
                                        admitted to non-ICU.
    - hospitalization: probability a person gets hospitalized due to
                       covid-19, including co-occurrence of infection and
                       admission to hospital.
    - hospitalization_icu: probability a person gets admitted to ICU due
                           to covid-19, including co-occurrence of infection
                           and admission to ICU.
    - hospitalization_general: probability a person gets admitted to non-ICU
                               due to covid-19, including co-occurrence of
                               infection and admission to non-ICU.
    - IFR: probability an infected person dies of covid-19.
    """

    HOSPITALIZATION_GENERAL_INFECTED = 'hospitalization_general_infected'
    HOSPITALIZATION_ICU_INFECTED = 'hospitalization_icu_infected'
    HOSPITALIZATION_INFECTED = 'hospitalization_infected'
    HOSPITALIZATION_GENERAL = 'hospitalization_general'
    HOSPITALIZATION_ICU = 'hospitalization_icu'
    HOSPITALIZATION = 'hospitalization'
    IFR = 'IFR'


class CovidMeasureUnit(Enum):
    PER_CAPITA = 'per_capita'
    PER_CAPITA_DAY = 'per_capita_day'


class DemographicMapper:
    """
    Maps SEIR model inference to a target population based on the target
    population's demographic distribution. Currently supports mapping based
    on age structure.

    The mapper calculates:
    i. size of population at each state of infection (susceptible,
    exposed, infected etc.) which are predicted by the MLE model and mapped
    to target demographic distribution (currently supports age groups);
    ii. probabilities of hospitalization or death by given demographic
    category, depending on the measure.
    For detailed description of measure's meanings, check CovidMeasure.

    The measure may quantify the probability of a future outcome or a
    daily event, depending on the measure_unit: when measure unit is `per
    capita`, the measure quantifies the probability that an future event will
    ultimately occur; when measure unit is `per capita day`, the measure
    quantifies the probability of an event per day.

    The final results include:
    i. time series of population size at different states of infection
       summed over demographic groups and mapped to the target demographic
       distribution.
    ii. time series of measures averaged through demographic groups weighted
        by the target demographic distribution. If risk_modifier_by_age is
        specified, it will be used as relative risk of target population
        compared to general population risk per age group to further modifies
        the weights.

    Attributes
    ----------
    predictions: dict
        Contains time series of SEIR model compartment size summed
        over age groups. Time series are simulated with MLE parameters.
        With keys:
        - 'E': np.array, exposed
        - 'I': np.array, infected and symptomatic
        - 'A': np.array, infected and asymptomatic
        - 'HGen': np.array, admitted to non-ICU
        - 'HICU': np.array, admitted to ICU
        - 'HVent': np.array, admitted to ICU with ventilator
        - 'D': np.array, death during hospitalization
        - 'R': recovered
    predictions_by_age: dict
        Contains time series of SEIR model compartment size by age groups.
        Time series are simulated with MLE parameters.
        With keys:
        - 'E': np.array, exposed by age groups
        - 'I': np.array, infected and symptomatic by age groups
        - 'A': np.array, infected and asymptomatic by age groups
        - 'HGen': np.array, admitted to non-ICU by age groups
        - 'HICU': np.array, admitted to ICU by age groups
        - 'HVent': np.array, admitted to ICU with ventilator by age groups
    parameters: dict
        Contains MLE model parameters. For full list of parameters,
        refer description of parameters of SEIRModelAge.
    measures: list(CovidMeasure)
        Covid measures.
    measure_units: list(CovidMeasureUnit)
        Units of covid measure.
    hospitalization_rates: dict
        Rates of hospitalization by age group, type of hospitalization,
        and unit of rates, with type of hospitalization as primary key,
        unit as secondary key, and array of corresponding time series of
        rates as values.
        For example, hospitalization_rates['HGen']['per_capita'] is the
        time series of probability of being admitted to non-ICU per
        capita among infected population (asymptomatic + symptomatic).
    mortality_rates: dict
        Rates of mortality by age group, type of hospitalization,
        and unit of rates, with type of hospitalization as primary key,
        unit as secondary key, and array of corresponding time series of
        rates as values.
        For example, mortality_rates['HICU']['per_capita'] is the time
        series of probability of death in ICU per capita among infected
        population (asymptomatic + symptomatic + hospitalized).
    prevalence: np.array
        Age-specific prevalence time series simulated with SEIR model
        with MLE parameters.
    results: dict
        Contains:
            - compartments:
              - <compartment>: time series of population at a specific
                               infection states (susceptible, infected,
                               hospitalized, etc.) simulated by MLE model
                               assuming the population has the demographic
                               distribution of the target population. Each time
                               series is recorded as pd.DataFrame, with dates of
                               prediction as index.
              name of compartment include: S - susceptible, E - exposed,
              A - asymptomatic, I - symptomatic, HGen - in non-ICU, HICU -
              in ICU, HVent - on ventilator, N - entire population.
            - <measure>:
              - <measure_unit>: time series of covid measures predicted using
                                the MLE model and averaged over target
                                demographic distribution (adjusted by risk
                                modification if relative risk is specified).
                                The time series is recorded as pd.DataFrame,
                                with dates of prediction as index.



    Parameters
    ----------
    fips: str
        State of county FIPS code
    mle_model: SEIRModelAge
        Model with age structure and MLE model parameters
    fit_results: dict
        MLE epi parameters and associated errors. The parameter used is
        t0_date.
    measures: str or list(str)
        Names of covid measures, should be interpretable by CovidMeasure.
    measure_units: str or list(str)
        Units of covid measures, should be interpretable by CovidMeasureUnit.
    target_age_distribution: callable
        PDF of age of target population.
    risk_modifier_by_age: callable
        Returns risk rations by age group that modifies the risk of
        hospitalization or mortality.
    """

    def __init__(self,
                 fips,
                 mle_model,
                 fit_results,
                 measures=None,
                 measure_units=None,
                 target_age_distribution=None,
                 risk_modifier_by_age=None):

        self.fips = fips
        self.predictions = {k: v for k, v in mle_model.results.items() if k != 'by_age'}
        self.predictions_by_age = mle_model.results['by_age']
        self.parameters = {k: v for k, v in mle_model.__dict__.items() if k not in ('by_age', 'results')}
        self.fit_results = fit_results

        if measures is not None:
            measures = [measures] if not isinstance(measures,list) else measures
            measures = [CovidMeasure(m) for m in measures]
        self.measures = measures

        if measure_units is not None:
            measure_units = [measure_units] if not isinstance(measure_units,list) else measure_units
            measure_units = [CovidMeasureUnit(u) for u in measure_units]
        self.measure_units = measure_units

        if target_age_distribution is None:
            target_age_distribution = lambda x: np.array([1] * len(self.parameters['age_groups']))
        self.target_age_distribution = target_age_distribution
        self.risk_modifier_by_age = risk_modifier_by_age

        # get parameters required to calculate covid measures
        hospitalization_rates, mortality_rates = self._generate_hospitalization_mortality_rates()
        self.hospitalization_rates = hospitalization_rates
        self.mortality_rates = mortality_rates
        self.prevalence = self._age_specific_prevalence()

        self.results = None


    def _age_specific_prevalence(self):
        """
        Calculate age specific prevalence.

        Returns
        -------
        prevalence: np.array
            Trajectory of covid infection prevalence inferred by the MLE
            model.
        """
        prevalence = (self.predictions_by_age['I']
                    + self.predictions_by_age['A'])/ self.parameters['N'][:, np.newaxis]
        return prevalence

    def _reconstruct_mortality_rates_trajectory(self):
        """
        Reconstruct trajectory of inferred per-capita mortality rates through
        time from MLE model parameters and compartments.

        Returns
        -------
        mortality_rate_general: np.array
            Age specific per-capita mortality rate in non-ICU.
        mortality_rate_icu: np.array
            Age specific per-capita mortality rate in ICU.
        mortality_rate_icu_vent: np.array
            Age specific per-capita mortality rate in ICU with ventilator.
        """

        mortality_rate_ICU = np.tile(self.parameters['mortality_rate_from_ICU'],
                                     (self.parameters['t_list'].shape[0], 1)).T
        mortality_rate_ICU[:, np.where(self.predictions['HICU'] > self.parameters['beds_ICU'])] = \
            self.parameters['mortality_rate_no_ICU_beds']

        mortality_rate_NonICU = np.tile(self.parameters['mortality_rate_from_hospital'],
                                        (self.parameters['t_list'].shape[0], 1)).T
        mortality_rate_NonICU[:, np.where(self.predictions['HGen'] > self.parameters['beds_general'])] = \
            self.parameters['mortality_rate_no_general_beds']

        mortality_rate_general = mortality_rate_NonICU / self.parameters['hospitalization_length_of_stay_general']
        mortality_rate_icu = (1 - self.parameters['fraction_icu_requiring_ventilator']) * mortality_rate_ICU / \
                             self.parameters['hospitalization_length_of_stay_icu']
        mortality_rate_icu_vent = self.parameters['mortality_rate_from_ICUVent'] / \
                                  self.parameters['hospitalization_length_of_stay_icu_and_ventilator']

        return mortality_rate_general, mortality_rate_icu, mortality_rate_icu_vent

    def _reconstruct_hospitalization_rates_trajectory(self):
        """
        Reconstruct trajectory of inferred per-capita hospitalization rates
        through time from MLE model parameters and compartments.

        Returns
        -------
        hospital_rate_general: np.array
            Age specific per-capita rate of admission to non-ICU.
        hospital_rate_icu: np.array
            Age specific per-capita rate of admission to ICU.
        hospital_rate_icu_vent: np.array
            Age specific per-capita rate of admission to ICU with ventilator.
        """

        hospital_rate_general = \
            (self.parameters['hospitalization_rate_general']
           - self.parameters['hospitalization_rate_icu']) / self.parameters['symptoms_to_hospital_days']
        hospital_rate_icu = self.parameters['hospitalization_rate_icu'] / self.parameters['symptoms_to_hospital_days']
        hospital_rate_ventilator = hospital_rate_icu * self.parameters['fraction_icu_requiring_ventilator']

        return hospital_rate_general, hospital_rate_icu, hospital_rate_ventilator

    def _reconstruct_recovery_rates_trajectory(self):
        """
        Reconstruct trajectory of inferred per-capita rates of recovery
        during hospitalization through time from MLE model parameters and
        compartments.

        Returns
        -------
        hospital_recovery_rate_general: np.array
            Age-specific rate of recovery in non-ICU.
        hospital_recovery_rate_icu: np.array
            Age-specific rate of recovery in ICU.
        hospital_recovery_rate_icu_vent: np.array
            Age-specific rate of recovery in ICU with ventilator.
        """
        mortality_rate_general, mortality_rate_icu, mortality_rate_icu_vent = \
            self._reconstruct_mortality_rates_trajectory()

        hospital_recovery_rate_general = (1 - mortality_rate_general) / \
            self.parameters['hospitalization_length_of_stay_general']
        hospital_recovery_rate_icu = (1 - mortality_rate_icu) * (1 - self.parameters['fraction_icu_requiring_ventilator']) \
            / self.parameters['hospitalization_length_of_stay_icu']
        hospital_recovery_rate_icu_vent = \
            (1 - np.maximum(mortality_rate_icu, self.parameters['mortality_rate_from_ICUVent'])) \
            / self.parameters['hospitalization_length_of_stay_icu_and_ventilator']

        return hospital_recovery_rate_general, hospital_recovery_rate_icu, hospital_recovery_rate_icu_vent

    def _age_specific_hospitalization_rates_among_infected(self, measure_unit):
        """
        Calculate age specific hospitalization rates among infected
        population for a given measure unit.

        When unit is per capita, calculates the probability that an infected
        person (asymptomatic or symptomatic) ultimately gets admitted to
        hospital:
            probability of being symptomatic * rate of hospitalized / (rate of
            hospitalized + rate of recovery without hospitalization)

        When unit is per capita day, calculate the probability that an
        infected person gets admitted to hospital per day:
           new hospitalization / (asymptomatic + symptomatic infections)

        Parameters
        ----------
        measure_unit: CovidMeasureUnit
            Unit of covid measure

        Returns
        -------
          : np.array
            Rates of admission to non-ICU of infected population
          : np.array
            Rates of admission to ICU of infected population
          : np.array
            Rates of admission to ICU with Ventilator of infected population
        """

        hospital_rate_general, hospital_rate_icu, hospital_rate_ventilator = \
            self._reconstruct_hospitalization_rates_trajectory()

        total_rate_out_of_I = self.parameters['delta'] + hospital_rate_general + hospital_rate_icu + hospital_rate_ventilator

        hospital_general_prob = np.tile(hospital_rate_general / total_rate_out_of_I,
                                        (len(self.parameters['t_list']), 1)).T

        hospital_icu_prob = np.tile(hospital_rate_icu / total_rate_out_of_I,
                                    (len(self.parameters['t_list']), 1)).T
        hospital_ventilator_prob = np.tile(hospital_rate_ventilator / total_rate_out_of_I,
                                           (len(self.parameters['t_list']), 1)).T

        fraction_of_symptomatic = self.predictions_by_age['I'] / (self.predictions_by_age['I']
                                                                + self.predictions_by_age['A'])

        if measure_unit is CovidMeasureUnit.PER_CAPITA:
            # calculate probability that an infected person will ultimately
            # be hospitalized
            return (hospital_general_prob * fraction_of_symptomatic,
                    hospital_icu_prob * fraction_of_symptomatic,
                    hospital_ventilator_prob * fraction_of_symptomatic)

        elif measure_unit is CovidMeasureUnit.PER_CAPITA_DAY:
            # calculate probability that an infected person
            # become hospitalized per day.
            return (hospital_rate_general[:, np.newaxis] * fraction_of_symptomatic,
                    hospital_rate_icu[:, np.newaxis] * fraction_of_symptomatic,
                    hospital_rate_ventilator[:, np.newaxis] * fraction_of_symptomatic)

    def _age_specific_mortality_rates_among_infected(self, measure_unit, hospitalization_rates):
        """
        Calculates age specific mortality with given unit among infected
        population.

        When unit is per capita, mortality rate is calculated as the
        probability that an infected person dies of covid-19:
            probability of hospitalization * mortality rate /
            (mortality rate + recovery rate)

        When unit is per capita day, mortality rate is calculated as
        probability of dying of covid-19 per capita per day among infected
        population:
            new death / (asymptomatic + symptomatic + hospitalized)

        Parameters
        ----------
        measure_unit: CovidMeasureUnit
            Unit of the covid measure.

        Returns
        -------
          : np.array
            Rates of mortality in non-ICU of infected population
          : np.array
            Rates of mortality in ICU of infected population
          : np.array
            Rates of mortality in ICU with Ventilator of infected population
        """
        mortality_rate_general, mortality_rate_icu, mortality_rate_icu_vent = \
            self._reconstruct_mortality_rates_trajectory()

        if measure_unit is CovidMeasureUnit.PER_CAPITA:
            hospital_general_recovery_rate, hospital_icu_recovery_rate, hospital_icu_vent_recovery_rate = \
                self._reconstruct_recovery_rates_trajectory()

            mortality_prob_general = mortality_rate_general/(mortality_rate_general + hospital_general_recovery_rate)
            mortality_prob_icu = mortality_rate_icu/(mortality_rate_icu + hospital_icu_recovery_rate)
            mortality_prob_icu_vent = mortality_rate_icu_vent/(mortality_rate_icu_vent + hospital_icu_vent_recovery_rate)

            return (mortality_prob_general * hospitalization_rates['HGen'][measure_unit.value],
                    mortality_prob_icu * hospitalization_rates['HICU'][measure_unit.value],
                    mortality_prob_icu_vent * hospitalization_rates['HVent'][measure_unit.value])

        elif measure_unit is CovidMeasureUnit.PER_CAPITA_DAY:
            total_infections = np.zeros(self.predictions_by_age['I'].shape)
            for c in ['I', 'A', 'HGen', 'HICU', 'HVent']:
                total_infections += self.predictions_by_age[c]

            died_from_hosp = mortality_rate_general * self.predictions_by_age['HGen']
            died_from_icu = mortality_rate_icu * self.predictions_by_age['HICU']
            died_from_icu_vent = mortality_rate_icu_vent * self.predictions_by_age['HVent']

            return (died_from_hosp/total_infections,
                    died_from_icu/total_infections,
                    died_from_icu_vent/total_infections)

    def _generate_hospitalization_mortality_rates(self):
        """
        Generates age specific hospitalization rates and mortality rates
        among infected population for given unit (per capita or per capita day).

        Returns
        -------
        hospitalization_rates: dict
            Rates of hospitalization by age group, type of hospitalization,
            and unit of rates, with unit as primary key, type of
            hospitalization as secondary key, and array of corresponding
            time series of rates as values.
            For example, hospitalization_rates['HGen']['per_capita'] is the
            time series of probability of being admitted to non-ICU per
            capita among infected population (asymptomatic + symptomatic).
        mortality_rates: dict
            Rates of mortality by age group, type of hospitalization,
            and unit of rates, with type of hospitalization as
            primary key, measure unit as secondary key, and array of
            corresponding time series of rates as values.
            For example, mortality_rates['HICU']['per_capita'] is the time
            series of probability of death in ICU per capita among infected
            population (asymptomatic + symptomatic + hospitalized).
        """

        hospitalization_rates = defaultdict(dict)
        mortality_rates = defaultdict(dict)
        for measure_unit in self.measure_units:
            hospitalization_rates['HGen'][measure_unit.value], \
            hospitalization_rates['HICU'][measure_unit.value], \
            hospitalization_rates['HVent'][measure_unit.value] =  \
                self._age_specific_hospitalization_rates_among_infected(measure_unit)

            mortality_rates['HGen'][measure_unit.value], \
            mortality_rates['HICU'][measure_unit.value], \
            mortality_rates['HVent'][measure_unit.value] = \
                self._age_specific_mortality_rates_among_infected(measure_unit, hospitalization_rates)

        return hospitalization_rates, mortality_rates

    def _calculate_age_specific_HR(self, measure, measure_unit):
        """
        Calculates age specific hospitalization rates given the specified
        covid measure and unit. The measure determines the types of
        hospitalizations to count and measure unit determines which
        hospitalization rates to use.

        Parameters
        ----------
        measure: CovidMeasure
            Specifies the measure to calculate.
        measure_unit: CovidMeasureUnit
            Specifies the measure's unit.

        Returns
        -------
          : np.array
            Array of time series of age-specific hospitalization rates of
            infected population or general population.
        """
        if measure is CovidMeasure.HOSPITALIZATION_INFECTED:
            return (self.hospitalization_rates['HGen'][measure_unit.value]
                  + self.hospitalization_rates['HICU'][measure_unit.value]
                  + self.hospitalization_rates['HVent'][measure_unit.value])

        elif measure is CovidMeasure.HOSPITALIZATION:
            # prevalence is used to count the probability that a person is
            # infected
            return (self.hospitalization_rates['HGen'][measure_unit.value]
                  + self.hospitalization_rates['HICU'][measure_unit.value]
                  + self.hospitalization_rates['HVent'][measure_unit.value]) * self.prevalence

        elif measure is CovidMeasure.HOSPITALIZATION_GENERAL_INFECTED:
            return self.hospitalization_rates['HGen'][measure_unit.value]

        elif measure is CovidMeasure.HOSPITALIZATION_GENERAL:
            # prevalence is used to count the probability that a person is
            # infected
            return self.hospitalization_rates['HGen'][measure_unit.value] * self.prevalence

        elif measure is CovidMeasure.HOSPITALIZATION_ICU_INFECTED:
            return (self.hospitalization_rates['HICU'][measure_unit.value]
                  + self.hospitalization_rates['HVent'][measure_unit.value])

        elif measure is CovidMeasure.HOSPITALIZATION_ICU:
            # prevalence is used to count the probability that a person is
            # infected
            return (self.hospitalization_rates['HICU'][measure_unit.value]
                  + self.hospitalization_rates['HVent'][measure_unit.value]) * self.prevalence

        else:
            logging.warnings(f'covid_measure {measure.value} is not relevant to hospitalization rate')
            return None

    def _calculate_age_specific_IFR(self, measure_unit):
        """
        Calculates age specific infection fatality rate (IFR).

        Parameters
        ----------
        measure_unit: CovidMeasureUnit
            Unit of IFR, determines how IFR is calculated.

        Returns
        -------
        IFR: np.array
            Array of time series of age-specific infection fatality rate.
        """

        IFR = 0
        for key in self.mortality_rates:
            IFR += self.mortality_rates[key][measure_unit.value]

        return IFR


    def generate_predictions(self):
        """
        Generates predictions of covid measures using the MLE model.

        Returns
        -------
        predictions: dict
            Contains:
            - compartments:
              - <compartment>: time series of population at a specific
                               infection states (susceptible, infected,
                               hospitalized, etc.) by demographic group
                               simulated by MLE model. Each time series is
                               recorded as pd.DataFrame, with dates of
                               prediction as index.
              name of compartment include: S - susceptible, E - exposed,
              A - asymptomatic, I - symptomatic, HGen - in non-ICU, HICU - in
              ICU, HVent - on ventilator, N - entire population.
            - <measure>:
              - <measure_unit>: time series of covid measures by demographic
                                group predicted using the MLE model.
                                The time series is recorded as pd.DataFrame,
                                with dates of prediction as index.
        """
        predictions = defaultdict(dict)
        t0_date = datetime.fromisoformat(self.fit_results['t0_date'])
        dates = [t0_date + timedelta(days=int(t)) for t in self.parameters['t_list']]
        age_groups = ['-'.join([str(int(tup[0])), str(int(tup[1]))]) for tup in
                      self.parameters['age_groups']]

        for c in self.predictions_by_age:
            predictions['compartments'][c] = pd.DataFrame(self.predictions_by_age[c].T,
                                                          columns=age_groups,
                                                          index=pd.DatetimeIndex(dates))
        # assuming a stable demographic distribution through time
        predictions['compartments']['N'] = \
            pd.DataFrame(np.tile(self.parameters['N'], (len(self.parameters['t_list']), 1)),
                         columns=age_groups,
                         index=pd.DatetimeIndex(dates))

        if self.measures is not None:
            for measure in self.measures:
                for measure_unit in self.measure_units:
                    if measure is CovidMeasure.IFR:
                        predictions[measure.value][measure_unit.value] = self._calculate_age_specific_IFR(measure_unit)
                    else:
                        predictions[measure.value][measure_unit.value] = self._calculate_age_specific_HR(measure,
                                                                                                         measure_unit)
                    predictions[measure.value][measure_unit.value] = \
                        pd.DataFrame(predictions[measure.value][measure_unit.value].T,
                                     columns=age_groups,
                                     index=pd.DatetimeIndex(dates))


        return predictions


    def map_to_target_population(self, predictions):
        """
        Maps the age-specific covid measures predicted by the MLE model to
        the target age distribution.

        Parameters
        ----------
        predictions: dict
            Contains:
            - compartments:
              - <compartment>: time series of population at a specific
                               infection states (susceptible, infected,
                               hospitalized, etc.) by demographic group
                               simulated by MLE model. Each time series is
                               recorded as pd.DataFrame, with dates of
                               prediction as index.
              name of compartment include: S - susceptible, E - exposed,
              A - asymptomatic, I - symptomatic, HGen - in non-ICU, HICU - in
              ICU, HVent - on ventilator, N - entire population.
            - <measure>:
              - <measure_unit>: time series of covid measures by demographic
                                group predicted using the MLE model.
                                The time series is recorded as pd.DataFrame,
                                with dates of prediction as index.

        Returns
        -------
          : dict
            Contains:
            - compartments:
              - <compartment>: time series of population at a specific
                               infection states (susceptible, infected,
                               hospitalized, etc.) simulated by MLE model
                               assuming the population has the demographic
                               distribution of the target population. Each time
                               series is recorded as pd.DataFrame, with dates of
                               prediction as index.
              name of compartment include: S - susceptible, E - exposed,
              A - asymptomatic, I - symptomatic, HGen - in non-ICU, HICU - in ICU,
              HVent - on ventilator, N - entire population.
            - <measure>:
              - <measure_unit>: time series of covid measures predicted using
                                the MLE model and averaged over target
                                demographic distribution (adjusted by risk
                                modification if relative risk is specified).
                                The time series is recorded as pd.DataFrame,
                                with dates of prediction as index.
        """
        # calculate weights
        age_bin_centers = [np.mean(tup) for tup in self.parameters['age_groups']]
        weights = self.target_age_distribution(age_bin_centers)
        if (weights != 1).sum() == 0:
            logging.warning('no target age distribution is given, measure is aggregated assuming age '
                            'distrubtion at given FIPS code')

        weights /= weights.sum()
        demographic_group_size_ratio = weights / (self.parameters['N'] / self.parameters['N'].sum())

        mapped_predictions = defaultdict(dict)

        for c in predictions['compartments']:
            mapped_predictions['compartments'][c] = predictions['compartments'][c].dot(demographic_group_size_ratio)

        measure_names = [k for k in predictions.keys() if k != 'compartments']
        if len(measure_names) > 0:
            for measure_name in measure_names:
                for measure_unit_name in predictions[measure_name]:
                    modified_weights = weights
                    if self.risk_modifier_by_age is not None:
                        if measure_name in self.risk_modifier_by_age_group:
                            modified_weights = weights * self.risk_modifier_by_age[measure_name](age_bin_centers)
                            modified_weights /= modified_weights.sum()
                    mapped_predictions[measure_name][measure_unit_name] = \
                        predictions[measure_name][measure_unit_name].dot(modified_weights)

        self.results = mapped_predictions

        return self.results

    def run(self):
        """
        Makes predictions of age-specific population size at each state of
        infection and covid measures using the MLE model and maps them to
        the target age distribution.

        Returns
        -------
          : dict
            Contains:
            - compartments:
              - <compartment>: time series of population at a specific
                               infection states (susceptible, infected,
                               hospitalized, etc.) simulated by MLE model
                               assuming the population has the demographic
                               distribution of the target population. Each time
                               series is recorded as pd.DataFrame, with dates of
                               prediction as index.
              name of compartment include: S - susceptible, E - exposed,
              A - asymptomatic, I - symptomatic, HGen - in non-ICU, HICU - in
              ICU, HVent - on ventilator, N - entire population.
            - <measure>:
              - <measure_unit>: time series of covid measures predicted using
                                the MLE model and averaged over target
                                demographic distribution (adjusted by risk
                                modification if relative risk is specified).
                                The time series is recorded as pd.DataFrame,
                                with dates of prediction as index.
        """
        predictions = self.generate_predictions()
        self.results = self.map_to_target_population(predictions)
        return self.results
