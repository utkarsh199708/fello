"""``python -m app rescore <score_id>``: recompute a stored score from its model version (task 4.8).

It loads the score row, the snapshot it was computed from and the ``model_versions`` artifact it
names, scores the stored feature vector again, and checks every stored number:

- the probability and the margin match what the model gives now;
- every per-feature contribution matches;
- expected value plus the sum of contributions equals the stored margin.

Exit 0 when everything reconciles to within ``TOLERANCE``, 1 otherwise. It reads as ``app_batch``
(score ids are global, and this is an operator tool, not a request path) and writes nothing.
Run it in the worker image: it imports the model libraries, which the web process never loads.
"""

from __future__ import annotations

import argparse
import math
from uuid import UUID

import pandas as pd
import sqlalchemy as sa

from app.db import get_engine
from app.ml.scorer import scorer_from_artifact

TOLERANCE = 1e-9

ROW_SQL = sa.text(
    """
    SELECT sc.score_id, sc.tenant_id, sc.account_id, sc.as_of, sc.model_version,
           sc.probability, sc.margin, sc.expected_value, sc.contributions,
           sn.features, sn.feature_set_version, m.kind, m.artifact, m.features AS model_features
    FROM scores sc
    JOIN snapshots sn ON sn.snapshot_id = sc.snapshot_id
    JOIN model_versions m ON m.model_version = sc.model_version
    WHERE sc.score_id = :score_id
    """
)


def rescore(score_id: UUID | str) -> dict:
    """Recompute one score. Returns the stored and recomputed values and every check's result."""
    with get_engine("app_batch").connect() as conn:
        row = conn.execute(ROW_SQL, {"score_id": UUID(str(score_id))}).one_or_none()
    if row is None:
        raise LookupError(f"no score {score_id}")
    scorer = scorer_from_artifact(row.kind, row.artifact)
    X = pd.DataFrame.from_records([row.features], columns=list(row.model_features)).astype(
        "float64"
    )
    expected, contributions = scorer.contributions(X)
    margin = float(scorer.margin(X)[0])
    probability = float(scorer.score(X)[0])
    contrib = {f: float(v) for f, v in contributions.iloc[0].items()}
    differs = {
        f: (row.contributions.get(f), v)
        for f, v in contrib.items()
        if not math.isclose(row.contributions.get(f, math.nan), v, abs_tol=TOLERANCE)
    }
    checks = {
        "probability": math.isclose(row.probability, probability, abs_tol=TOLERANCE),
        "margin": math.isclose(row.margin, margin, abs_tol=TOLERANCE),
        "expected_value": math.isclose(row.expected_value, expected, abs_tol=TOLERANCE),
        "contributions": not differs,
        "sum_reconciles": math.isclose(
            row.expected_value + sum(row.contributions.values()), row.margin, abs_tol=1e-6
        ),
    }
    return {
        "score_id": str(row.score_id),
        "model_version": row.model_version,
        "as_of": row.as_of.isoformat(),
        "stored": {"probability": row.probability, "margin": row.margin},
        "recomputed": {"probability": probability, "margin": margin, "expected_value": expected},
        "contribution_differences": differs,
        "checks": checks,
        "ok": all(checks.values()),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app rescore")
    parser.add_argument("score_id")
    args = parser.parse_args(argv[1:])
    result = rescore(args.score_id)
    print(f"score {result['score_id']}  model {result['model_version']}  as of {result['as_of']}")
    for name in ("probability", "margin"):
        print(
            f"  {name:<12} stored {result['stored'][name]:.12f}"
            f"  recomputed {result['recomputed'][name]:.12f}"
        )
    for name, ok in result["checks"].items():
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    for feature, (stored, now) in result["contribution_differences"].items():
        print(f"       {feature}: stored {stored} recomputed {now}")
    print("reconciled" if result["ok"] else "DOES NOT RECONCILE")
    return 0 if result["ok"] else 1
