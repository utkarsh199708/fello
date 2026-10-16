"""Scorer tests: contributions add up to the margin, NULL contributes nothing, artifacts round-trip.

Pure: synthetic data, no database.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from app.ml.scorer import LogisticScorer, scorer_from_artifact

FEATURES = ("x1", "x2", "x3", "constant")


@pytest.fixture
def data():
    rng = np.random.default_rng(3)
    n = 400
    X = pd.DataFrame(
        {
            "x1": rng.normal(0, 1, n),
            "x2": rng.normal(50, 10, n),
            "x3": rng.normal(0, 1, n),
            "constant": np.ones(n),
        }
    )
    y = pd.Series((X["x1"] - 0.1 * (X["x2"] - 50) + rng.normal(0, 1, n) > 0).astype(int))
    X.loc[::7, "x3"] = np.nan  # some NULLs
    return X, y


def test_expected_value_plus_contributions_equals_margin(data):
    X, y = data
    scorer = LogisticScorer(FEATURES).fit(X, y)
    expected_value, contributions = scorer.contributions(X)
    assert list(contributions.columns) == list(FEATURES)
    np.testing.assert_allclose(
        expected_value + contributions.sum(axis=1), scorer.margin(X), rtol=0, atol=1e-12
    )
    np.testing.assert_allclose(scorer.score(X), 1 / (1 + np.exp(-scorer.margin(X))))
    assert ((scorer.score(X) > 0) & (scorer.score(X) < 1)).all()


def test_null_and_constant_features_contribute_nothing(data):
    X, y = data
    scorer = LogisticScorer(FEATURES).fit(X, y)
    _, contributions = scorer.contributions(X)
    assert (contributions.loc[X["x3"].isna(), "x3"] == 0).all()
    assert (contributions["constant"] == 0).all()


def test_the_model_learns_the_signal(data):
    X, y = data
    scorer = LogisticScorer(FEATURES).fit(X, y)
    coef = dict(zip(FEATURES, scorer.coef, strict=True))
    assert coef["x1"] > 0 and coef["x2"] < 0


def test_artifact_round_trip_scores_identically(data):
    X, y = data
    scorer = LogisticScorer(FEATURES).fit(X, y)
    artifact = json.loads(json.dumps(scorer.to_artifact()))  # as it comes back from jsonb
    again = scorer_from_artifact("logreg", artifact)
    assert again.features == scorer.features
    np.testing.assert_array_equal(again.score(X), scorer.score(X))
    np.testing.assert_array_equal(again.margin(X), scorer.margin(X))


def test_columns_are_read_by_name(data):
    X, y = data
    scorer = LogisticScorer(FEATURES).fit(X, y)
    shuffled = X[list(reversed(X.columns))].assign(extra=1.0)
    np.testing.assert_array_equal(scorer.score(shuffled), scorer.score(X))


def test_fit_is_deterministic(data):
    X, y = data
    a = LogisticScorer(FEATURES).fit(X, y).to_artifact()
    b = LogisticScorer(FEATURES).fit(X, y).to_artifact()
    assert a == b


def test_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        scorer_from_artifact("lightgbm", {})
