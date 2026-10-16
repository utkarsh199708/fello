"""Step 4 end to end on config/small.yaml: two linked model versions, reproducible scores, and an
evaluation whose leakage probes pass.

The small config has 36 months of history, so unlike the tiny fixture it has real training and
test cycles. One module-scoped run (about a minute) is shared by every test here.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa

from app.config import load_config
from app.db import get_engine, tenant_tx
from app.evaluation.evaluate import evaluate
from app.generator.generate import generate
from app.ingest.loader import load_all
from app.ingest.resolve import resolve
from app.ml.features import write_snapshots
from app.ml.labels import build_anchors
from app.ml.rescore import rescore
from app.ml.score import score_tenant
from app.ml.train import choose, train
from tests.helpers import ROOT, purge_tenants

SMALL = ROOT / "config" / "small.yaml"


@pytest.fixture(scope="module")
def small(database_url, tmp_path_factory):
    cfg = load_config(SMALL)
    data = tmp_path_factory.mktemp("small") / "data"
    generate(cfg, data)
    load_all(data)
    start = cfg.horizon_start + timedelta(weeks=cfg.anchor_warmup_weeks)
    for t in cfg.tenants:
        with tenant_tx(t.tenant_id) as conn:
            resolve(conn, t.tenant_id, cfg.train_as_of)
            build_anchors(conn, t.tenant_id, start, cfg.train_as_of)
    version = train(cfg)
    for t in cfg.tenants:
        with tenant_tx(t.tenant_id) as conn:
            write_snapshots(conn, t.tenant_id, cfg.train_as_of, "nightly")
            score_tenant(conn, t.tenant_id, cfg.train_as_of)
    yield cfg, data, version
    purge_tenants(database_url, [t.tenant_id for t in cfg.tenants])


def versions():
    with get_engine("app_batch").connect() as conn:
        return {
            r.role: r
            for r in conn.execute(
                sa.text("SELECT * FROM model_versions WHERE feature_set_version = 'fs4'"
                        " AND (is_active OR model_version IN"
                        " (SELECT validated_by FROM model_versions WHERE is_active))")
            )
        }  # fmt: skip


def test_an_evaluation_fit_and_a_live_refit_are_stored_and_linked(small):
    cfg, _, version = small
    rows = versions()
    live, evaluation = rows["live"], rows["evaluation"]
    assert live.model_version == version and live.is_active and not evaluation.is_active
    assert live.validated_by == evaluation.model_version
    # One kind for both, chosen on the validation tail by the rule in app.ml.train.choose.
    selection = evaluation.metrics["selection"]
    assert set(selection["validation_log_loss"]) == {"lightgbm", "logreg"}
    assert live.kind == evaluation.kind == selection["chosen"]
    assert selection["chosen"] == choose(
        selection["validation_log_loss"], selection["validation_cycles"]
    )
    assert live.metrics["selection"] == selection
    # The evaluation fit saw nothing after the holdout start; the live refit sees everything.
    assert evaluation.label_cutoff < live.label_cutoff == cfg.train_as_of
    assert evaluation.anchor_last < evaluation.label_cutoff
    for row in (live, evaluation):
        assert set(row.tenant_row_counts) == {str(t.tenant_id) for t in cfg.tenants}
        assert row.metrics["num_threads"] == 2
        if row.kind == "lightgbm":
            assert row.artifact["booster"].startswith("tree")
            params = row.artifact["params"]
            assert params["deterministic"] and params["force_row_wise"]
            assert params["num_threads"] == row.metrics["num_threads"]
    if live.kind == "lightgbm":  # the refit uses the rounds the evaluation fit stopped at
        assert live.metrics["num_boost_round"] == evaluation.metrics["num_boost_round"]
    holdout = evaluation.metrics["holdout"]
    assert holdout["rows"] > 0 and 0 < holdout["positives"] < holdout["rows"]


def test_training_again_gives_the_same_versions(small):
    cfg, _, version = small
    before = versions()["evaluation"].model_version
    assert train(cfg) == version  # deterministic: same rows, same booster text, same hash
    assert versions()["evaluation"].model_version == before


def test_every_stored_score_is_reproduced_by_rescore(small):
    cfg, _, _ = small
    with tenant_tx(cfg.tenants[0].tenant_id) as conn:
        ids = (
            conn.execute(
                sa.text("SELECT score_id FROM scores WHERE as_of = :d ORDER BY score_id LIMIT 25"),
                {"d": cfg.train_as_of},
            )
            .scalars()
            .all()
        )
    assert ids
    for score_id in ids:
        result = rescore(score_id)
        assert result["ok"], result


@pytest.fixture(scope="module")
def report(small):
    cfg, data, _ = small
    return evaluate(cfg, data)


def test_the_evaluation_has_five_rows_and_passes_its_leakage_probes(report):
    assert list(report["comparison"]) == [
        "oracle",
        "lightgbm",
        "logistic regression",
        "rules",
        "days to renewal",
    ]
    leak = report["leakage"]
    assert leak["short_lead_ok"] and leak["shuffled_ok"], leak
    # Not a calendar: the live kind beats ranking by renewal date alone.
    rows = report["comparison"]
    assert rows[report["live_kind"]]["pr_auc"] > rows["days to renewal"]["pr_auc"]
    assert report["calendar"]["top_reason_share"] < 0.5
    # The oracle is the ceiling, and it is not perfect: some churn is unobserved by design.
    assert rows["oracle"]["roc_auc"] < 0.9


def test_planted_cases_surface_before_the_notice_deadline(report):
    """A regression floor, not a realism claim: measured 7 of 10 on this config."""
    planted = report["planted"]
    assert len(planted) == 10
    in_time = sum(p["lead_days"] is not None and p["lead_days"] >= 28 for p in planted)
    assert in_time >= 6, planted


def test_the_model_choice_needs_a_clear_win_on_enough_cycles():
    close = {"lightgbm": 0.4736, "logreg": 0.4718}  # full config: 0.4% apart
    assert choose(close, cycles=207) == "lightgbm"
    clear = {"lightgbm": 0.50, "logreg": 0.48}
    assert choose(clear, cycles=207) == "logreg"
    assert choose(clear, cycles=22) == "lightgbm"  # too few cycles to trust the tail
