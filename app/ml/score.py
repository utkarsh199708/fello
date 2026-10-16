"""Score one tenant's snapshots with the active model and rank each CSM's book.

A score row stores everything needed to explain it without re-running the model: probability,
margin, ``expected_value`` and the per-feature contributions (which add up to the margin), the
model version, and the snapshot it was computed from.

Ranking. ``rank_score = probability * arr`` is the expected ARR at risk, so a likely loss on a
small account does not outrank a moderate risk on a large one. ``rank_in_book`` numbers a CSM's
accounts 1, 2, 3 ... by ``rank_score`` descending (ties broken by account id so the order is
stable). Accounts with no CSM form one book of their own.

Functions here take ``conn`` and never commit: the caller wraps them in ``tenant_tx``.
"""

from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

import pandas as pd
import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.db import copy_insert
from app.ids import stable_uuid
from app.ml.feature_spec import FEATURE_SET_VERSION, FEATURES
from app.ml.scorer import Scorer, scorer_from_artifact

SCORE_COLUMNS = (
    "score_id",
    "tenant_id",
    "account_id",
    "snapshot_id",
    "model_version",
    "as_of",
    "csm_id",
    "probability",
    "margin",
    "expected_value",
    "contributions",
    "arr",
    "days_to_renewal",
    "rank_score",
    "rank_in_book",
    "k",
    "run_id",
)


def active_model(conn: Connection) -> tuple[str, Scorer]:
    """The active model's version and scorer. Fails loudly if there is none or it is stale."""
    row = conn.execute(
        sa.text(
            "SELECT model_version, kind, feature_set_version, artifact"
            " FROM model_versions WHERE is_active"
        )
    ).one_or_none()
    if row is None:
        raise LookupError("no active model: run `python -m app train` first")
    if row.feature_set_version != FEATURE_SET_VERSION:
        raise LookupError(
            f"active model {row.model_version} uses feature set {row.feature_set_version},"
            f" but this code builds {FEATURE_SET_VERSION}: retrain first"
        )
    return row.model_version, scorer_from_artifact(row.kind, row.artifact)


def rank_books(frame: pd.DataFrame) -> pd.Series:
    """Position of each row within its CSM's book by rank_score descending, starting at 1.

    ``frame`` needs csm_id (None for unassigned), account_id and rank_score.
    """
    ordered = frame.assign(_key=frame["account_id"].astype(str)).sort_values(
        ["rank_score", "_key"], ascending=[False, True]
    )
    ranks = ordered.groupby("csm_id", dropna=False, sort=False).cumcount() + 1
    return ranks.reindex(frame.index)


def score_tenant(
    conn: Connection, tenant_id: UUID | str, as_of: datetime, run_id: UUID | None = None
) -> int:
    """Score every snapshot of the tenant at ``as_of``; return how many score rows were new.

    ``ON CONFLICT DO NOTHING`` on (tenant, account, as_of, model_version): scoring the same night
    with the same model twice writes nothing the second time.
    """
    tid = UUID(str(tenant_id))
    version, scorer = active_model(conn)
    k = conn.execute(
        sa.text("SELECT top_k FROM tenants WHERE tenant_id = :tid"), {"tid": tid}
    ).scalar_one()
    rows = conn.execute(
        sa.text(
            """
            SELECT s.snapshot_id, s.account_id, s.features, a.csm_id
            FROM snapshots s
            JOIN accounts a ON a.account_id = s.account_id
            WHERE s.tenant_id = :tid AND s.as_of = :as_of AND s.feature_set_version = :fsv
            ORDER BY s.account_id
            """
        ),
        {"tid": tid, "as_of": as_of, "fsv": FEATURE_SET_VERSION},
    ).all()
    if not rows:
        return 0

    X = pd.DataFrame.from_records([r.features for r in rows], columns=list(FEATURES)).astype(
        "float64"
    )
    expected_value, contributions = scorer.contributions(X)
    frame = pd.DataFrame(
        {
            "account_id": [r.account_id for r in rows],
            "snapshot_id": [r.snapshot_id for r in rows],
            "csm_id": pd.Series([r.csm_id for r in rows], dtype=object),
            "probability": scorer.score(X),
            "margin": scorer.margin(X),
            "arr": X["arr"],
            "days_to_renewal": X["days_to_renewal"],
        }
    )
    frame["rank_score"] = frame["probability"] * frame["arr"]
    frame["rank_in_book"] = rank_books(frame)

    out = (
        (
            stable_uuid("score", tid, r.account_id, as_of.isoformat(), version),
            tid,
            r.account_id,
            r.snapshot_id,
            version,
            as_of,
            r.csm_id,
            float(r.probability),
            float(r.margin),
            float(expected_value),
            json.dumps({f: float(v) for f, v in contributions.loc[r.Index].items()}),
            float(r.arr),
            int(r.days_to_renewal),
            float(r.rank_score),
            int(r.rank_in_book),
            int(k),
            run_id,
        )
        for r in frame.itertuples()
    )
    return copy_insert(conn, "scores", SCORE_COLUMNS, out, on_conflict="ON CONFLICT DO NOTHING")
