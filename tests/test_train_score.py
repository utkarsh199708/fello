"""Training and scoring against the seeded tiny tenants.

Asserts the ``model_versions`` row, that retraining on the same rows only re-activates the same
version, and that score rows reconcile (contributions add up to the margin, rank_score is
probability times ARR, ranks run 1..n within each CSM's book).
"""

from __future__ import annotations

import math
import uuid
from datetime import timedelta

import pandas as pd
import pytest
import sqlalchemy as sa

from app.config import load_config
from app.db import get_engine, tenant_tx
from app.ml import cli
from app.ml.feature_spec import FEATURE_SET_VERSION, FEATURES
from app.ml.score import rank_books, score_tenant
from app.ml.scorer import scorer_from_artifact
from app.ml.train import train
from tests.helpers import TINY, purge_tenants, seed_tiny

CFG = load_config(TINY)


@pytest.fixture(scope="module")
def seeded(database_url, tmp_path_factory):
    """Seed the tiny tenants; purge them afterwards, since tenant slugs are unique per database."""
    ids = seed_tiny(tmp_path_factory.mktemp("tiny"))
    yield ids
    purge_tenants(database_url, ids)


def active_rows():
    with get_engine("app_batch").connect() as conn:
        return conn.execute(sa.text("SELECT * FROM model_versions WHERE is_active")).all()


def test_model_versions_row(seeded):
    (row,) = active_rows()
    assert row.model_version.startswith("lightgbm-") and row.kind == "lightgbm"
    assert row.role == "live"
    # The tiny history is too short for a 12-month holdout, so no evaluation fit was made,
    # and the live version says so instead of pointing at one.
    assert row.validated_by is None and row.metrics["evaluation"].startswith("skipped")
    assert row.feature_set_version == FEATURE_SET_VERSION
    assert row.features == list(FEATURES)
    assert row.train_as_of == CFG.train_as_of and row.label_cutoff == CFG.train_as_of
    assert row.anchor_first <= row.anchor_last < CFG.train_as_of
    assert row.anchor_first.weekday() == 0
    assert set(row.tenant_row_counts) == {str(t) for t in seeded}
    fit = row.metrics["in_sample"]
    assert sum(row.tenant_row_counts.values()) == fit["rows"]
    assert 0 < fit["positives"] < fit["rows"]
    assert 0.5 < fit["roc_auc"] <= 1
    assert row.metrics["num_threads"] == row.artifact["params"]["num_threads"]
    assert scorer_from_artifact(row.kind, row.artifact).features == FEATURES


def test_retraining_the_same_rows_reactivates_the_same_version(seeded):
    (before,) = active_rows()
    assert train(CFG) == before.model_version
    (after,) = active_rows()
    assert after.model_version == before.model_version
    assert after.created_at == before.created_at  # not rewritten


def test_score_rows_reconcile(seeded):
    (model,) = active_rows()
    for tid in seeded:
        with tenant_tx(tid) as conn:
            scores = conn.execute(
                sa.text(
                    """
                    SELECT sc.*, s.as_of AS snapshot_as_of, s.features
                    FROM scores sc JOIN snapshots s ON s.snapshot_id = sc.snapshot_id
                    WHERE sc.as_of = :as_of
                    """
                ),
                {"as_of": CFG.train_as_of},
            ).all()
            snapshots = conn.execute(
                sa.text("SELECT count(*) FROM snapshots WHERE as_of = :as_of"),
                {"as_of": CFG.train_as_of},
            ).scalar_one()
        assert len(scores) == snapshots > 0
        for s in scores:
            assert s.model_version == model.model_version and s.snapshot_as_of == s.as_of
            assert set(s.contributions) == set(FEATURES)
            assert s.expected_value + sum(s.contributions.values()) == pytest.approx(
                s.margin, abs=1e-9
            )
            assert s.probability == pytest.approx(1 / (1 + math.exp(-s.margin)))
            assert float(s.arr) == s.features["arr"]
            assert s.days_to_renewal == s.features["days_to_renewal"]
            assert s.rank_score == pytest.approx(s.probability * float(s.arr))
            assert s.k == 8
            # Thin accounts are scored but not ranked, and shown grey (task 4.5).
            thin = s.features["account_tenure_days"] < 30 or s.features["total_event_count"] < 5
            assert s.evidence_state == ("insufficient" if thin else "scored")
            assert (s.rank_in_book is None) == thin and (s.colour == "grey") == thin
            if not thin:
                expected = (
                    "red" if s.rank_in_book <= 8 else "yellow" if s.rank_in_book <= 24 else "green"
                )
                assert s.colour == expected
            assert s.coverage["total_event_count"] == s.features["total_event_count"]

        frame = pd.DataFrame([s._asdict() for s in scores])
        ranked = frame[frame["evidence_state"] == "scored"]
        for _, book in ranked.groupby("csm_id"):
            ordered = book.sort_values("rank_in_book")
            assert list(ordered["rank_in_book"]) == list(range(1, len(book) + 1))
            assert ordered["rank_score"].is_monotonic_decreasing


def test_scoring_again_writes_nothing(seeded):
    with tenant_tx(seeded[0]) as conn:
        assert score_tenant(conn, seeded[0], CFG.train_as_of) == 0


def test_unassigned_accounts_form_one_book():
    frame = pd.DataFrame(
        {
            "csm_id": pd.Series([None, "c1", None, "c1", None], dtype=object),
            "account_id": [uuid.UUID(int=n) for n in range(5)],
            "rank_score": [5.0, 4.0, 3.0, 9.0, 3.0],
        }
    )
    # Unassigned book: 5.0 -> 1, then the 3.0 tie broken by account id -> 2, 3.
    assert list(rank_books(frame)) == [1, 2, 2, 1, 3]


def test_cli_anchors_train_score(seeded, capsys):
    assert cli.main(["anchors", "--config", str(TINY), "--tenant", "t2"]) == 0
    assert "anchor snapshots written=0" in capsys.readouterr().out
    assert cli.main(["train", "--config", str(TINY)]) == 0
    (model,) = active_rows()
    assert model.model_version in capsys.readouterr().out

    next_day = CFG.train_as_of + timedelta(days=1)
    assert cli.main(["score", "--as-of", next_day.isoformat(), "--tenant", "t2"]) == 0
    with tenant_tx(seeded[1]) as conn:
        kinds = conn.execute(
            sa.text(
                "SELECT s.kind, count(*) FROM scores sc JOIN snapshots s USING (snapshot_id)"
                " WHERE sc.as_of = :as_of GROUP BY s.kind"
            ),
            {"as_of": next_day},
        ).all()
    assert len(kinds) == 1 and kinds[0][0] == "nightly" and kinds[0][1] > 0
