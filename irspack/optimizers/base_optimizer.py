import logging
import re
import time
from abc import ABCMeta
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

import optuna
import pandas as pd

from irspack.utils.default_logger import get_default_logger

from ..evaluator import Evaluator
from ..parameter_tuning import Suggestion, is_valid_param_name, overwrite_suggestions
from ..recommenders.base import BaseRecommender, InteractionMatrix
from ..recommenders.base_earlystop import BaseRecommenderWithEarlyStopping


class BaseOptimizer(object, metaclass=ABCMeta):
    """The base optimizer class for recommender classes.

    The child class must define

        - ``recommender_class``
        - ``default_tune_range``

    Args:
        data (Union[scipy.sparse.csr_matrix, scipy.sparse.csc_matrix]):
            The train data.
        val_evaluator (Evaluator):
            The validation evaluator which measures the performance of the recommenders.
        logger (Optional[logging.Logger], optional):
            The logger used during the optimization steps. Defaults to None.
            If ``None``, the default logger of irspack will be used.
        suggest_overwrite (List[Suggestion], optional):
            Customizes (e.g. enlarging the parameter region or adding new parameters to be tuned)
            the default parameter search space defined by ``default_tune_range``
            Defaults to list().
        fixed_params (Dict[str, Any], optional):
            Fixed parameters passed to recommenders during the optimization procedure.
            If such a parameter exists in :obj:`default_tune_range`, it will not be tuned.
            Defaults to dict().

    """

    recommender_class: Type[BaseRecommender]
    default_tune_range: List[Suggestion] = []

    def __init__(
        self,
        data: InteractionMatrix,
        val_evaluator: Evaluator,
        logger: Optional[logging.Logger] = None,
        suggest_overwrite: List[Suggestion] = list(),
        fixed_params: Dict[str, Any] = dict(),
    ):

        if logger is None:
            logger = get_default_logger()

        self.logger = logger
        self._data = data
        self.val_evaluator = val_evaluator

        self.current_trial: int = 0
        self.best_val = float("inf")

        self.suggestions = overwrite_suggestions(
            self.default_tune_range, suggest_overwrite, fixed_params
        )
        self.fixed_params = fixed_params

    def _suggest(self, trial: optuna.Trial) -> Dict[str, Any]:
        parameters: Dict[str, Any] = dict()
        for s in self.suggestions:
            parameters[s.name] = s.suggest(trial)
        return parameters

    def get_model_arguments(
        self, *args: Any, **kwargs: Any
    ) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        return args, kwargs

    def _objective_function(
        self,
    ) -> Callable[[optuna.Trial], float]:
        def objective_func(trial: optuna.Trial) -> float:
            start = time.time()
            params = dict(**self._suggest(trial), **self.fixed_params)
            self.logger.info("Trial %s:", trial.number)
            self.logger.info("parameter = %s", params)

            arg, parameters = self.get_model_arguments(**params)

            recommender = self.recommender_class(self._data, *arg, **parameters)
            recommender.learn_with_optimizer(self.val_evaluator, trial)

            score = self.val_evaluator.get_score(recommender)
            end = time.time()

            time_spent = end - start
            self.logger.info(
                "Config %d obtained the following scores: %s within %f seconds.",
                trial.number,
                score,
                time_spent,
            )
            val_score = score[self.val_evaluator.target_metric.name]

            # max_epoch will be stored in learnt_config
            for param_name, param_val in recommender.learnt_config.items():
                trial.set_user_attr(param_name, param_val)

            score_history: List[
                Tuple[int, Dict[str, float]]
            ] = trial.study.user_attrs.get("scores", [])
            score_history.append(
                (
                    trial.number,
                    {
                        f"{score_name}@{self.val_evaluator.cutoff}": score_value
                        for score_name, score_value in score.items()
                    },
                )
            )
            trial.study.set_user_attr("scores", score_history)
            return -val_score

        return objective_func

    def optimize_with_study(
        self,
        study: optuna.Study,
        n_trials: int = 20,
        timeout: Optional[int] = None,
    ) -> Tuple[Dict[str, Any], pd.DataFrame]:
        """Perform the optimization step using the user-created ``optuna.Study`` object.
        Creating and managing the study object will be convenient e.g. when you

            1. want to `store/resume the study using RDB backend <https://optuna.readthedocs.io/en/stable/tutorial/003_rdb.html>`_.
            2. want perform a `distributed optimization <https://optuna.readthedocs.io/en/stable/tutorial/004_distributed.html>`_.

        Args:
            study:
                The study object.
            n_trials:
                The number of expected trials (include pruned trial.). Defaults to 20.
            timeout:
                If set to some value (in seconds), the study will exit after that time period.
                Note that the running trials is not interrupted, though. Defaults to None.

        Returns:
            A tuple that consists of

                1. A dict containing the best paramaters.
                   This dict can be passed to the recommender as ``**kwargs``.
                2. A ``pandas.DataFrame`` that contains the history of optimization.

        """

        objective_func = self._objective_function()

        self.logger.info(
            """Start parameter search for %s over the range: %s""",
            type(self).recommender_class.__name__,
            self.suggestions,
        )

        study.optimize(objective_func, n_trials=n_trials, timeout=timeout)
        best_params = dict(
            **study.best_trial.params,
            **{
                key: val
                for key, val in study.best_trial.user_attrs.items()
                if is_valid_param_name(key)
            },
        )
        result_df = study.trials_dataframe().set_index("number")

        # remove prefix
        result_df.columns = [
            re.sub(r"^(user_attrs|params)_", "", colname)
            for colname in result_df.columns
        ]

        trial_and_scores: List[Tuple[float, Dict[str, float]]] = study.user_attrs.get(
            "scores", []
        )
        score_df = pd.DataFrame(
            [x[1] for x in trial_and_scores],
            index=[x[0] for x in trial_and_scores],
        )
        score_df.index.name = "number"
        result_df = result_df.join(score_df, how="left")
        return best_params, result_df

    def optimize(
        self,
        n_trials: int = 20,
        timeout: Optional[int] = None,
        random_seed: Optional[int] = None,
    ) -> Tuple[Dict[str, Any], pd.DataFrame]:
        """Perform the optimization step.
        ``optuna.Study`` object is created inside this function.

        Args:
            n_trials:
                The number of expected trials (include pruned trial.). Defaults to 20.
            timeout:
                If set to some value (in seconds), the study will exit after that time period.
                Note that the running trials is not interrupted, though. Defaults to None.
            random_seed:
                The random seed to control ``optuna.samplers.TPESampler``. Defaults to None.

        Returns:
            A tuple that consists of

                1. A dict containing the best paramaters.
                   This dict can be passed to the recommender as ``**kwargs``.
                2. A ``pandas.DataFrame`` that contains the history of optimization.

        """
        study = optuna.create_study(
            sampler=optuna.samplers.TPESampler(seed=random_seed)
        )
        return self.optimize_with_study(study, n_trials, timeout)


class BaseOptimizerWithEarlyStopping(BaseOptimizer):
    """The Base Optimizer class for early-stoppable recommenders.

    The child class must define

        - ``recommender_class``
        - ``default_tune_range``


    Args:
        data (Union[scipy.sparse.csr_matrix, scipy.sparse.csc_matrix]):
            The train data.
        val_evaluator (Evaluator):
            The validation evaluator which measures the performance of the recommenders.
        logger (Optional[logging.Logger], optional):
            The logger used during the optimization steps. Defaults to None.
            If ``None``, the default logger of irspack will be used.
        suggest_overwrite (List[Suggestion], optional):
            Customizes (e.g. enlarging the parameter region or adding new parameters to be tuned)
            the default parameter search space defined by ``default_tune_range``
            Defaults to list().
        fixed_params (Dict[str, Any], optional):
            Fixed parameters passed to recommenders during the optimization procedure.
            If such a parameter exists in ``default_tune_range``, it will not be tuned.
            Defaults to dict().
        max_epoch (int, optional):
            The maximal number of epochs for the training. Defaults to 512.
        validate_epoch (int, optional):
            The frequency of validation score measurement. Defaults to 5.
        score_degradation_max (int, optional):
            Maximal number of allowed score degradation. Defaults to 5. Defaults to 5.
    """

    recommender_class: Type[BaseRecommenderWithEarlyStopping]

    def __init__(
        self,
        data: InteractionMatrix,
        val_evaluator: Evaluator,
        logger: Optional[logging.Logger] = None,
        suggest_overwrite: List[Suggestion] = list(),
        fixed_params: Dict[str, Any] = dict(),
        max_epoch: int = 512,
        validate_epoch: int = 5,
        score_degradation_max: int = 5,
        **kwargs: Any,
    ):

        super().__init__(
            data,
            val_evaluator,
            logger=logger,
            suggest_overwrite=suggest_overwrite,
            fixed_params=fixed_params,
        )
        self.max_epoch = max_epoch
        self.validate_epoch = validate_epoch
        self.score_degradation_max = score_degradation_max

    def get_model_arguments(
        self, *args: Any, **kwargs: Any
    ) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        return super().get_model_arguments(
            *args,
            max_epoch=self.max_epoch,
            validate_epoch=self.validate_epoch,
            score_degradation_max=self.score_degradation_max,
            **kwargs,
        )
