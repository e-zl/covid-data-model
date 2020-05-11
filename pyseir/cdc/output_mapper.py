import itertools
import os
import us
import numpy as np
import pandas as pd
from collections import defaultdict
from datetime import datetime, timedelta
from multiprocessing import Pool
from epiweeks import Week, Year
from string import Template
from pyseir import OUTPUT_DIR, load_data
from pyseir.utils import REF_DATE
from pyseir.cdc.utils import Target, ForecastTimeUnit, ForecastUncertainty, target_column_name
from pyseir.inference.fit_results import load_inference_result, load_mle_model
from pyseir.ensembles.ensemble_runner import EnsembleRunner


"""
This mapper maps current pyseir model output to match cdc format.

Output file should have columns:
- forecast_date: the date on which the submitted forecast data was made available in YYYY-MM-DD format
- target: Values in the target column must be a character (string) and have format "<day_num> day ahead <target_measure>"
          where day_num is number of days since forecast_date to each date in forecast time range. 
- target_end_date: end date of forecast in YYYY-MM-DD format.
- location: 2 digit FIPS code
- type: "quantile" or "point"
- quantile: quantiles of forecast target measure, with format 0.###.
- value: value of target measure at given quantile and forecast date for given location.
and optional: 
- location_name: name of the location that can be useful to identify the location. 

For details on formatting, check:
https://github.com/reichlab/covid19-forecast-hub/blob/master/data-processed/README.md

The output includes:
- <forecast_date>_<team>_<model>_<fips>.csv
  File that contain the output with above columns for a specific fips.
- <forecast_date>_<team>_<model>.csv
  File that contain the output with above columns for all US states fips.
- metadata-CovidActNow.txt
  Metadata with most up-to-date forecast date.
  
Where default value of forecast_date, team and model can be found from corresponding global variables.
"""


TEAM = 'CovidActNow'
MODEL = 'SEIR_CAN'

# type of target measures
TARGETS = ['cum death', 'inc death', 'inc hosp']

# names of target measures that will be used to generate metadata
TARGETS_TO_NAMES = {'cum death': 'cumulative deaths',
                    'inc death': 'incident deaths',
                    'inc hosp': 'incident hospitalizations'}

# units of forecast target.
FORECAST_TIME_UNITS = ['day', 'wk']
# number of weeks ahead for forecast.
FORECAST_WEEKS_NUM = 4
# Default quantiles required by CDC.
QUANTILES = np.concatenate([[0.01, 0.025], np.arange(0.05, 0.95, 0.05), [0.975, 0.99]])
# Time of forecast, default date when this runs.
FORECAST_DATE = datetime.today()
# Next epi week. Epi weeks starts from Sunday and ends on Saturday.
#if forecast date is Sunday or Monday, next epi week is the week that starts
#with the latest Sunday.
if FORECAST_DATE.weekday() in (0, 6):
    NEXT_EPI_WEEK = Week(Year.thisyear().year, Week.thisweek().week)
else:
    NEXT_EPI_WEEK = Week(Year.thisyear().year, Week.thisweek().week + 1)
COLUMNS = ['forecast_date', 'location', 'location_name', 'target',
           'target_end_date', 'quantile', 'value']

DIR_PATH = os.path.dirname(os.path.realpath(__file__))
OUTPUT_FOLDER = os.path.join(OUTPUT_DIR, 'pyseir', 'cdc')


class OutputMapper:
    """
    This mapper maps CAN SEIR model inference results to the format required
    for CDC model submission. For the given State FIPS code, it reads in the
    most up-to-date MLE inference (mle model + fit_results json file),
    and runs the model ensemble when fixing the parameters varied for model
    fitting at their MLE estimate. Quantile of forecast is then derived from
    the forecast ensemble weighted by the chi square obtained by fitting
    corresponding model to observed cases, deaths w/o hospitalizations,
    aiming to obtain the uncertainty associated with the prior distribution
    of the parameters not varied during model fitting and the (
    unknown) likelihood function (L(y_1:t|theta)). This for sure will
    underestimate the level of uncertainty of the forecast since it does not
    take into account the parameters varied for MLE inference.
    However, the error associated with the parameters for inference is
    generally small, so their variations are also relatively small.

    The output has the columns required for CDC model ensemble
    submission (check description of results). It currently supports daily
    and weekly forecast.

    Attributes
    ----------
    model: SEIRModel
        SEIR model with MLE parameter estimates.
    forecast_given_time_range: callable
        Makes forecast of a target during the forecast time window given a
        model's prediction of target and model's t_list (t steps since the
        inferred starting time of the epidemic, t0).
    targets: list(Target)
        List of Target objects.
    forecast_time_units: list(ForecastTimeUnit)
        List of ForecastTimeUnit objects, determines whether forecast target
        is aggregated by day or week. Currently the mapper only supports
        daily forecasts.
    forecast_uncertainty: ForecastUncertainty
        Determines how forecast uncertainty is adjusted based on number of
        days the forecast is made ahead and total days of observations.
        Should be interpretable by ForecastUncertainty. Currently supports:
        - ForecastUncertainty.DEFAULT: no adjustment
        - ForecastUncertainty.NAIVE: rescale the standard deviation by factor (1
                                     + days_ahead  ** 0.5)
    result: pd.DataFrame
        Output that meets requirement for CDC model ensemble submission for
        given FIPS code.
        Contains columns:
        - forecast_date: datetime.datetime
          the date on which the submitted forecast data was made available in
          YYYY-MM-DD format
        - target: str
          Name of the forecast target, with format "<day_num> <unit>
          ahead <target_measure>" where day_num is number of days/weeks since
          forecast_date to each date in forecast time range, and unit is the
          time unit of the forecast, i.e. day or wk.
        - target_end_date: datetime.datetime
          last date of forecast in YYYY-MM-DD format.
        - location: str
          2 digit FIPS code.
        - type: str
          "quantile"
        - quantile: str
          quantiles of forecast target measure, with format 0.###.
        - value: float
          Value of target measure at given quantile and forecast date for
          given location.
        - location_name: str
          Name of the state.


    Parameters
    ----------
    fips: str
        State or County FIPS code
    N_samples: int
        Number SEIR model parameter sets of sample.
    targets: list(str)
        Names of the targets to forecast, should be interpretable by Target.
    forecast_time_units: list(str)
        Names of the time unit of forecast, should be interpretable by
        ForecastTimeUnit.
    forecast_date: datetime.date
        Date when the forecast is done, default the same day when the mapper
        runs.
    next_epi_week: epiweeks.week
        The coming epi weeks, with the start date of which the forecast time
        window begins.
    quantiles: list(float)
        Values between 0-1, which are the quantiles of the forecast target
        to collect. For default value check QUANTILES.
    forecast_uncertainty: str
        Determines how forecast uncertainty is adjusted based on number of
        days the forecast is made ahead and total days of observations.
        Should be interpretable by ForecastUncertainty. Currently supports:
        - 'default': no adjustment
        - 'naive': rescale the standard deviation by factor (1 + days_ahead
                   ** 0.5)
    """
    def __init__(self,
                 fips,
                 N_samples=5000,
                 targets=TARGETS,
                 forecast_time_units=FORECAST_TIME_UNITS,
                 forecast_date=FORECAST_DATE,
                 next_epi_week=NEXT_EPI_WEEK,
                 quantiles=QUANTILES,
                 forecast_uncertainty='default'):

        self.fips = fips
        self.N_samples = N_samples
        self.targets = [Target(t) for t in targets]
        self.forecast_time_units = [ForecastTimeUnit(u) for u in forecast_time_units]
        self.forecast_date = forecast_date
        self.forecast_time_range = [datetime.fromordinal(next_epi_week.startdate().toordinal()) + timedelta(n)
                                    for n in range(FORECAST_WEEKS_NUM * 7)]
        # remove past predictions
        self.forecast_time_range = [t for t in self.forecast_time_range if datetime.date(t) >
                                    datetime.date(self.forecast_date)]
        self.quantiles = quantiles
        self.forecast_uncertainty = ForecastUncertainty(forecast_uncertainty)

        self.model = load_mle_model(self.fips)

        self.fit_results = load_inference_result(self.fips)
        forecast_days_since_ref_date = [(t - REF_DATE).days for t in self.forecast_time_range]
        self.forecast_given_time_range = \
            lambda forecast, t_list: np.interp(forecast_days_since_ref_date,
                                               [self.fit_results['t0'] + t for t in t_list],
                                               forecast)

        self.result = None

    def run_model_ensemble(self, override_param_names=('R0', 'I_initial',
                                                       'E_initial',
                                                       'suppression_policy')):
        """
        Get model ensemble by running models under different parameter sets
        sampled from parameter prior distributions.

        Parameters
        ----------
        override_param_names: tuple(str)
            Names of model parameters to override. Default list includes the
            parameters varied for MLE inference.

        Returns
        -------
        model_ensemble: np.array(SEIRModel)
            SEIR models ran under parameter sets randomly generated
            from the parameter prior distributions.
        chi_squares: np.array(float)
            Chi squares when fitting each model in model_ensemble to
            to observed cases, deaths w/o hospitalizations.
        """

        override_params = {k: v for k, v in self.model.__dict__.items() if k in override_param_names}
        override_params.update({k: v for k, v in self.fit_results.items() if k in ['eps', 't_break', 'test_fraction']})
        er = EnsembleRunner(fips=self.fips)
        model_ensemble, chi_squares = er.model_ensemble(
            override_params=override_params, N_samples=self.N_samples, chi_square=True)
        model_ensemble = np.append([self.model], model_ensemble)
        chi_squares = np.append(sum([self.fit_results[k] for k in self.fit_results if k.startswith('chi2_')]),
                                chi_squares)

        return model_ensemble, chi_squares


    def forecast_target(self, model, target, unit):
        """
        Runs forecast of a target with given model.

        Parameters
        ----------
        model: SEIRModel
            SEIR model to run the forecast.
        target: Target
            The target to forecast.
        unit: ForecastTimeUnit
            Time unit to aggregate the forecast.

        Returns
        -------
        target_forecast: np.array
            Forecast of target at given unit (daily or weekly), with shape (
            len(self.forecast_time_range),)
        """

        if target is Target.INC_DEATH:
            target_forecast = self.forecast_given_time_range(model.results['total_deaths_per_day'], model.t_list)

        elif target is Target.INC_HOSP:
            target_forecast = self.forecast_given_time_range(np.append([0],
                                                             np.diff(model.results['HGen_cumulative']
                                                                   + model.results['HICU_cumulative'])),
                                                             model.t_list)

        elif target is Target.CUM_DEATH:
            target_forecast = self.forecast_given_time_range(model.results['D'], model.t_list)

        else:
            raise ValueError(f'Target {target} is not implemented')

        if unit is ForecastTimeUnit.DAY:
            num_of_units = [(datetime.date(t) - datetime.date(self.forecast_date)).days for t in
                            self.forecast_time_range]
            target_end_date = self.forecast_time_range
        elif unit is ForecastTimeUnit.WK:
            # n wk forecast are forecast for future Saturdays.
            saturdays = np.where([t.weekday() == 5 for t in self.forecast_time_range])[0]
            num_of_units = list(range(1, saturdays.shape[0] + 1))
            target_forecast = target_forecast[saturdays,]
            target_end_date = [self.forecast_time_range[n] for n in saturdays]
        else:
            raise ValueError(f'Forecast time unit {unit} is not implemented')

        target_forecast = pd.DataFrame(target_forecast,
                                       index=list(target_column_name(num_of_units, target, unit)))
        target_forecast['target_end_date'] = target_end_date
        target_forecast = target_forecast.set_index('target_end_date', append=True)

        return target_forecast

    def generate_forecast_ensemble(self, model_ensemble):
        """
        Generates a forecast ensemble given the model ensemble.

        Parameters
        ----------
        model_ensemble: list(SEIRModel) or NoneType
            List of SEIR models run under parameter sets randomly generated
            from the parameter prior distributions.

        Returns
        -------
        forecast_ensemble: dict(pd.DataFrame)
            Contains forecast of target within the forecast time window run by
            each model from the model ensemble. With "<num> <unit> ahead
            <target_measure>" as index, and corresponding value from each model
            as columns, where unit can be 'day' or 'wk' depending on the
            forecast_time_units.
        """

        forecast_ensemble = defaultdict(dict)
        for target in self.targets:
            for unit in self.forecast_time_units:
                forecast_ensemble[target.value][unit.value] = list()
                for model in model_ensemble:
                    target_forecast = self.forecast_target(model, target, unit).fillna(0)
                    forecast_ensemble[target.value][unit.value].append(target_forecast)
                forecast_ensemble[target.value][unit.value] = pd.concat(forecast_ensemble[target.value][unit.value],
                                                                        axis=1)
                # set all negative compartment size to zero
                forecast_ensemble[target.value][unit.value][forecast_ensemble[target.value][unit.value] < 0] = 0
        return forecast_ensemble

    def _adjust_forecast_dist(self, data, h, T):
        """
        Rescale forecast standard deviation by streching/shrinking the forecast
        distribution around the mean. Currently supports two approaches:
        - default: no adjustment
        - naive: rescale the standard deviation by factor (1 + days_ahead  **
                 0.5)

        Parameters
        ----------
        data: np.array or list
            Data sample from the distribution.
        h: int or float
            Time step of forecast
        T: int or float
            Total days of projection before the forecast starts.

        Returns
        -------
          :  np.array
            Data after the adjustment.
        """
        data = np.array(data)
        if self.forecast_uncertainty is ForecastUncertainty.DEFAULT:
            return data
        elif self.forecast_uncertainty is ForecastUncertainty.NAIVE:
            return (data + (data - data.mean()) * (1 + h ** 0.5)).clip(0)
        else:
            raise ValueError(f'forecast accuracy adjustment {self.forecast_uncertainty} is not implemented')

    def _weighted_quantiles(self, quantile, data, weights):
        """
        Calculate quantile of data with given weights.

        Parameters
        ----------
        quantile: np.array of list
            Quantile to find corresponding data value.
        data: np.array or list
            Data sample
        weights: np.array
            Weight of each data point.

        Returns
        -------
          :  np.array
            Value of data at given quantile.
        """
        # remove lower 10% and upper 10% percentile to increase stability.
        data = np.array(data)
        lower, upper = np.quantile(data, (0.1, 0.9))
        not_outlier = (data > lower) & (data < upper)
        data = data[not_outlier]
        weights = weights[not_outlier]
        sorted_idx = np.argsort(data)
        cdf = weights[sorted_idx].cumsum() / weights[sorted_idx].cumsum().max()

        return np.interp(quantile, cdf, data[sorted_idx])

    def generate_quantile_result(self, forecast_ensemble, chi_squares=None):
        """
        Generates results that contain the quantiles of the forecast with
        format required for CDC model ensemble submission.

        Parameters
        ----------
        forecast_ensemble: dict
            Contains forecast of target within the forecast time window run by
            each model from the model ensemble. With "<day_num> day ahead
            <target_measure>" as index, and corresponding value from each model
            as columns.
        chi_squares: np.array(float)
            Chi squares obtains by fitting each model (which makes the
            forecast in forecast ensemble) to observed cases, deaths w/o
            hospitalizations.


        Returns
        -------
        quantile_result: pd.DataFrame
            Contains the quantiles of the forecast with format required for
            CDC model ensemble submission. For info on columns,
            check description of self.results.
        """

        quantile_result = list()
        for target_name in forecast_ensemble:
            for unit_name in forecast_ensemble[target_name]:
                target_output = \
                    forecast_ensemble[target_name][unit_name].apply(
                        lambda l: self._weighted_quantiles(self.quantiles, l,
                                                           chi_squares.max() - chi_squares), axis=1)\
                                                             .rename('value')

                target_output = target_output.explode().reset_index().rename(columns={'level_0': 'target'})
                target_output['quantile'] = np.tile(['%.3f' % q for q in self.quantiles],
                                                    forecast_ensemble[target_name][unit_name].shape[0])
                quantile_result.append(target_output)

        quantile_result = pd.concat(quantile_result, axis=0)
        quantile_result['location'] = str(self.fips)
        quantile_result['location_name'] = us.states.lookup(self.fips).name
        quantile_result['type'] = 'quantile'
        quantile_result['forecast_date'] = self.forecast_date
        quantile_result['forecast_date'] = quantile_result['forecast_date'].dt.strftime('%Y-%m-%d')

        return quantile_result

    def run(self):
        """
        Runs forecast ensemble. Results contain quantiles of
        the forecast targets and saves results to csv file.

        Returns
        -------
          :  pd.DataFrame
          Output that meets requirement for CDC model ensemble submission for
          given FIPS code. Contains columns:
            - forecast_date: datetime.datetime
              the date on which the submitted forecast data was made available in
              YYYY-MM-DD format
            - target: str
              Name of the forecast target, with format "<day_num> <unit> ahead
              <target_measure>" where day_num is number of days/weeks since
              forecast_date to each date in forecast time range, and unit is
              the time unit of the forecast, i.e. day or wk.
            - target_end_date: datetime.datetime
              Date of forecast target in YYYY-MM-DD format.
            - location: str
              2 digit FIPS code.
            - type: str
              "quantile"
            - quantile: str
              quantiles of forecast target measure, with format 0.###.
            - value: float
              Value of target measure at given quantile and forecast date for
              given location.
            - location_name: str
              Name of the state.
        """
        models, chi_squares = self.run_model_ensemble()
        forecast_ensemble = self.generate_forecast_ensemble(models)
        self.result = self.generate_quantile_result(forecast_ensemble, chi_squares)
        forecast_date = self.forecast_date.strftime('%Y-%m-%d')
        self.result.to_csv(os.path.join(OUTPUT_FOLDER,
                                        f'{forecast_date}_{TEAM}_{MODEL}_{self.fips}.csv'),
                                        index=False)

        return self.result

    @classmethod
    def run_for_fips(cls, fips):
        """
        Run OutputMapper for given State FIPS code.

        Parameters
        ----------
        fips: str
            State FIPS code

        Returns
        -------
        results: pd.DataFrame
            Output that meets requirement for CDC model ensemble submission for
            given FIPS code. For details on columns, refer description of
            self.results.
        """
        om = cls(fips)
        result = om.run()
        return result

    @classmethod
    def generate_metadata(cls):
        """
        Generates metadata file based on the template.
        """
        om = cls(fips='06')
        with open(os.path.join(DIR_PATH, 'metadata-CovidActNow_template.txt'),  'r') as f:
            metadata = f.read()

        combined_target_names = list(itertools.product([u.value for u in om.forecast_time_units],
                                     [t.value for t in om.targets]))
        metadata = \
            Template(metadata).substitute(
            dict(Model_targets=', '.join([' ahead '.join(tup) for tup in combined_target_names]),
                 forecast_startdate=om.forecast_time_range[0].strftime('%Y-%m-%d'),
                 Model_target_names=', '.join([TARGETS_TO_NAMES[t.value] for t in om.targets]),
                 model_name=MODEL,
                 team_name=TEAM)
        )

        output_f = open(os.path.join(OUTPUT_FOLDER, f'metadata-{TEAM}-{MODEL}.txt'), 'w')
        output_f.write(metadata)
        output_f.close()


def run_all(parallel=False):
    """
    Prepares inference results for all whitelist States for CDC model
    ensemble submission.

    Parameters
    ----------
    parallel: bool
        Whether to run multiprocessing.
    """
    if not os.path.exists(OUTPUT_FOLDER):
        os.mkdir(OUTPUT_FOLDER)

    df_whitelist = load_data.load_whitelist()
    df_whitelist = df_whitelist[df_whitelist['inference_ok'] == True]
    fips_list = list(df_whitelist['fips'].str[:2].unique())

    if parallel:
        p = Pool()
        results = p.map(OutputMapper.run_for_fips, fips_list)
        p.close()
    else:
        results = list()
        for fips in fips_list:
            result = OutputMapper.run_for_fips(fips)
            results.append(result)

    forecast_date = FORECAST_DATE.strftime('%Y-%m-%d')

    results = pd.concat(results)
    results = results[COLUMNS].sort_values(COLUMNS)
    results.to_csv(os.path.join(OUTPUT_FOLDER, f'{forecast_date}-{TEAM}-{MODEL}.csv'),
                   index=False)

    OutputMapper.generate_metadata()
