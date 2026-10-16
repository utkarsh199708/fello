"""The scorer interface and the Step 1 model behind it: logistic regression.

Every scorer gives the same four answers for a feature matrix ``X`` (columns = ``features``):

- ``score(X)``: probability that the next renewal is lost or contracted;
- ``margin(X)``: the same on the log-odds scale;
- ``contributions(X)``: ``(expected_value, per-feature contributions)`` with
  ``expected_value + contributions.sum(axis=1) == margin(X)``, so the reasons shown to a CSM add
  up exactly to the number shown;
- ``to_artifact()``: everything needed to score again, as plain JSON, stored in
  ``model_versions.artifact``. ``scorer_from_artifact`` turns it back into a scorer.

``LightGBMScorer`` is the live model since Step 4; ``LogisticScorer`` stays as the comparison.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np
import pandas as pd


class Scorer(Protocol):
    kind: str
    features: tuple[str, ...]

    def fit(self, X: pd.DataFrame, y: pd.Series) -> Scorer: ...
    def score(self, X: pd.DataFrame) -> np.ndarray: ...
    def margin(self, X: pd.DataFrame) -> np.ndarray: ...
    def contributions(self, X: pd.DataFrame) -> tuple[float, pd.DataFrame]: ...
    def to_artifact(self) -> dict: ...


class LogisticScorer:
    """Impute NULL to the training mean, standardise, then a plain logistic regression.

    Standardising gives every feature the same scale, so ``coef * z`` is a fair per-feature
    contribution: ``z`` says how far from the training average this account sits, ``coef`` how
    much that distance moves the log-odds. A NULL feature is imputed to the mean, so its ``z`` is
    0 and it contributes nothing: "we could not see it" never pushes a score either way.
    ``expected_value`` is the intercept, the margin of an account that is average on everything.
    """

    kind = "logreg"

    def __init__(self, features: tuple[str, ...] | list[str], C: float = 1.0) -> None:
        self.features = tuple(features)
        self.C = C
        self.means: np.ndarray | None = None
        self.scales: np.ndarray | None = None
        self.coef: np.ndarray | None = None
        self.intercept: float | None = None

    def _z(self, X: pd.DataFrame) -> np.ndarray:
        """Standardised matrix with NULL as 0 (the training mean)."""
        values = X.loc[:, list(self.features)].to_numpy(dtype="float64")
        z = (values - self.means) / self.scales
        return np.where(np.isnan(z), 0.0, z)

    def fit(self, X: pd.DataFrame, y: pd.Series) -> LogisticScorer:
        # Imported here so that scoring with a stored model never loads scikit-learn.
        from sklearn.linear_model import LogisticRegression

        values = X.loc[:, list(self.features)].to_numpy(dtype="float64")
        with np.errstate(all="ignore"):  # an all-NULL column has no mean; it is set to 0 below
            means = np.nanmean(values, axis=0)
            scales = np.nanstd(values, axis=0)
        self.means = np.where(np.isnan(means), 0.0, means)
        # A constant column (say, every tenant has Gmail) would divide by zero; scale 1 leaves
        # it at z = 0, where it contributes nothing.
        self.scales = np.where(np.isnan(scales) | (scales == 0), 1.0, scales)
        model = LogisticRegression(C=self.C, max_iter=1000)
        model.fit(self._z(X), np.asarray(y, dtype="int64"))
        self.coef = model.coef_[0].astype("float64")
        self.intercept = float(model.intercept_[0])
        return self

    def margin(self, X: pd.DataFrame) -> np.ndarray:
        return self.intercept + self._z(X) @ self.coef

    def score(self, X: pd.DataFrame) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-self.margin(X)))

    def contributions(self, X: pd.DataFrame) -> tuple[float, pd.DataFrame]:
        return self.intercept, pd.DataFrame(
            self._z(X) * self.coef, index=X.index, columns=list(self.features)
        )

    def to_artifact(self) -> dict:
        """Plain JSON: Python floats round-trip exactly through json and jsonb."""
        return {
            "features": list(self.features),
            "C": self.C,
            "means": [float(v) for v in self.means],
            "scales": [float(v) for v in self.scales],
            "coef": [float(v) for v in self.coef],
            "intercept": self.intercept,
        }

    @classmethod
    def from_artifact(cls, artifact: dict) -> LogisticScorer:
        scorer = cls(artifact["features"], C=artifact["C"])
        scorer.means = np.asarray(artifact["means"], dtype="float64")
        scorer.scales = np.asarray(artifact["scales"], dtype="float64")
        scorer.coef = np.asarray(artifact["coef"], dtype="float64")
        scorer.intercept = float(artifact["intercept"])
        return scorer


class LightGBMScorer:
    """Gradient-boosted trees, the live model from Step 4 (docs/PLAN.md section 6).

    NULL stays NaN: LightGBM learns which way a missing value should go at each split, and "not
    connected" is also visible to it through the ``<source>_connected`` flags. Trained at the
    natural positive rate (no class weights), so probabilities stay calibrated.

    Reproducible: ``deterministic`` with ``force_row_wise`` (deterministic alone is not honoured
    without it), a fixed seed and a fixed thread count, all stored with the model. The same rows
    give the same booster text, and so the same model version.

    Reasons: ``booster.predict(pred_contrib=True)`` gives exact per-feature SHAP contributions
    plus the expected value in its last column, and they add up to the raw margin. No ``shap``
    package is needed.
    """

    kind = "lightgbm"
    NUM_THREADS = 2
    PARAMS = {
        "objective": "binary",
        "learning_rate": 0.05,
        "num_leaves": 15,
        "min_data_in_leaf": 200,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "lambda_l2": 1.0,
        "deterministic": True,
        "force_row_wise": True,
        "seed": 20261016,
        "num_threads": NUM_THREADS,
        "verbose": -1,
    }
    MAX_ROUNDS = 2000
    EARLY_STOPPING_ROUNDS = 100
    DEFAULT_ROUNDS = 300  # used only when there is no validation set to stop on

    def __init__(
        self,
        features: tuple[str, ...] | list[str],
        params: dict | None = None,
        num_boost_round: int | None = None,
    ) -> None:
        self.features = tuple(features)
        self.params = {**self.PARAMS, **(params or {})}
        self.num_boost_round = num_boost_round
        self.booster_text: str | None = None
        self._booster = None

    def _matrix(self, X: pd.DataFrame) -> np.ndarray:
        return X.loc[:, list(self.features)].to_numpy(dtype="float64")

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        X_valid: pd.DataFrame | None = None,
        y_valid: pd.Series | None = None,
    ) -> LightGBMScorer:
        """Fit; with a validation set, stop early on it and keep the best number of rounds."""
        import lightgbm as lgb

        params = dict(self.params)
        # A small training set (a test fixture) cannot fill leaves of 200 rows.
        params["min_data_in_leaf"] = int(min(params["min_data_in_leaf"], max(5, len(X) // 20)))
        names = list(self.features)
        train = lgb.Dataset(
            self._matrix(X), label=np.asarray(y, dtype="float64"), feature_name=names
        )
        if X_valid is not None and len(X_valid) and len(np.unique(y_valid)) == 2:
            valid = lgb.Dataset(
                self._matrix(X_valid), label=np.asarray(y_valid, dtype="float64"), reference=train
            )
            booster = lgb.train(
                params,
                train,
                num_boost_round=self.MAX_ROUNDS,
                valid_sets=[valid],
                callbacks=[lgb.early_stopping(self.EARLY_STOPPING_ROUNDS, verbose=False)],
            )
            self.num_boost_round = int(booster.best_iteration) or self.MAX_ROUNDS
        else:
            rounds = self.num_boost_round or self.DEFAULT_ROUNDS
            booster = lgb.train(params, train, num_boost_round=rounds)
            self.num_boost_round = rounds
        self.params = params
        self.booster_text = booster.model_to_string(num_iteration=self.num_boost_round)
        self._booster = lgb.Booster(model_str=self.booster_text)
        return self

    def _model(self):
        if self._booster is None:
            import lightgbm as lgb

            self._booster = lgb.Booster(model_str=self.booster_text)
        return self._booster

    def margin(self, X: pd.DataFrame) -> np.ndarray:
        return self._model().predict(self._matrix(X), raw_score=True)

    def score(self, X: pd.DataFrame) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-self.margin(X)))

    def contributions(self, X: pd.DataFrame) -> tuple[float, pd.DataFrame]:
        values = self._model().predict(self._matrix(X), pred_contrib=True)
        expected = float(values[0, -1]) if len(values) else 0.0
        return expected, pd.DataFrame(values[:, :-1], index=X.index, columns=list(self.features))

    def importance(self) -> dict[str, float]:
        """Share of total split gain per feature."""
        gain = self._model().feature_importance(importance_type="gain")
        total = float(gain.sum()) or 1.0
        return {f: float(g) / total for f, g in zip(self.features, gain, strict=True)}

    def to_artifact(self) -> dict:
        return {
            "features": list(self.features),
            "params": self.params,
            "num_boost_round": self.num_boost_round,
            "booster": self.booster_text,
        }

    @classmethod
    def from_artifact(cls, artifact: dict) -> LightGBMScorer:
        scorer = cls(artifact["features"], artifact["params"], artifact["num_boost_round"])
        scorer.booster_text = artifact["booster"]
        return scorer


SCORERS = {LogisticScorer.kind: LogisticScorer, LightGBMScorer.kind: LightGBMScorer}


def scorer_from_artifact(kind: str, artifact: dict) -> Scorer:
    """Rebuild a stored scorer from ``model_versions.kind`` and ``model_versions.artifact``."""
    if kind not in SCORERS:
        raise ValueError(f"unknown scorer kind {kind!r}; expected one of {sorted(SCORERS)}")
    return SCORERS[kind].from_artifact(artifact)
