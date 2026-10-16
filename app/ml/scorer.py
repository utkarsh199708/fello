"""The scorer interface and the Step 1 model behind it: logistic regression.

Every scorer gives the same four answers for a feature matrix ``X`` (columns = ``features``):

- ``score(X)``: probability that the next renewal is lost or contracted;
- ``margin(X)``: the same on the log-odds scale;
- ``contributions(X)``: ``(expected_value, per-feature contributions)`` with
  ``expected_value + contributions.sum(axis=1) == margin(X)``, so the reasons shown to a CSM add
  up exactly to the number shown;
- ``to_artifact()``: everything needed to score again, as plain JSON, stored in
  ``model_versions.artifact``. ``scorer_from_artifact`` turns it back into a scorer.

Step 4 adds LightGBM behind the same interface.
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


SCORERS = {LogisticScorer.kind: LogisticScorer}


def scorer_from_artifact(kind: str, artifact: dict) -> Scorer:
    """Rebuild a stored scorer from ``model_versions.kind`` and ``model_versions.artifact``."""
    if kind not in SCORERS:
        raise ValueError(f"unknown scorer kind {kind!r}; expected one of {sorted(SCORERS)}")
    return SCORERS[kind].from_artifact(artifact)
