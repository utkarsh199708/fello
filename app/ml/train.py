"""Train the one global model and record it in ``model_versions``.

Training is the single deliberate cross-tenant reader (docs/PLAN.md section 8), so it connects as
``app_batch``, the role with BYPASSRLS, and reads only the tenants named in the config. It can
read every tenant table but write only ``model_versions``.

Steps, all inside one transaction:

1. Read the anchor snapshots with ``as_of <= train_as_of``.
2. Read the contracts and the outcome events (``OUTCOME_EVENT_TYPES``) received by the label
   cutoff, which is ``train_as_of``: nothing that arrived later can shape a label.
3. Label the anchors (``label_anchors``) and drop the censored ones.
4. Fit ``LogisticScorer`` and store it. The version is a hash of the fitted artifact, so the same
   data gives the same version. If that version is already stored, it is only made active.
"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.config import Config
from app.db import get_engine
from app.ml.feature_spec import FEATURE_SET_VERSION, FEATURES
from app.ml.features import contracts_frame, utc
from app.ml.labels import label_anchors
from app.ml.scorer import LogisticScorer
from app.vocab import OUTCOME_EVENT_TYPES


def read_anchors(conn: Connection, tenant_ids: list[UUID], upto) -> pd.DataFrame:
    """Anchor snapshots up to ``upto``, one row each, features as float columns (NULL is NaN).

    Ordered by tenant, account and time, so the matrix (and therefore the fit) is the same on
    every run.
    """
    rows = conn.execute(
        sa.text(
            """
            SELECT tenant_id, account_id, as_of, features
            FROM snapshots
            WHERE tenant_id = ANY(:tids) AND kind = 'anchor'
              AND feature_set_version = :fsv AND as_of <= :upto
            ORDER BY tenant_id, account_id, as_of
            """
        ),
        {"tids": tenant_ids, "fsv": FEATURE_SET_VERSION, "upto": upto},
    ).all()
    keys = pd.DataFrame(
        [(r.tenant_id, r.account_id, r.as_of) for r in rows],
        columns=["tenant_id", "account_id", "as_of"],
    )
    keys["as_of"] = utc(keys["as_of"])
    features = pd.DataFrame.from_records([r.features for r in rows], columns=list(FEATURES)).astype(
        "float64"
    )
    return pd.concat([keys, features], axis=1)


def read_outcomes(conn: Connection, tenant_ids: list[UUID], label_cutoff) -> pd.DataFrame:
    """Outcome events received by the cutoff, with the payload fields the labeler reads."""
    # One "(:sN, :tN)" placeholder pair per outcome type. The SQL text is built from a constant;
    # the values themselves are bound parameters.
    pairs = sorted(OUTCOME_EVENT_TYPES)
    values = ", ".join(f"(:s{i}, :t{i})" for i in range(len(pairs)))
    params: dict = {"tids": tenant_ids, "cutoff": label_cutoff}
    for i, (source, event_type) in enumerate(pairs):
        params[f"s{i}"], params[f"t{i}"] = source, event_type
    rows = conn.execute(
        sa.text(
            f"""
            SELECT account_id, source, event_type, received_at,
                   attrs->>'stage' AS stage,
                   (attrs->>'renewal_date')::date AS renewal_date,
                   (attrs->>'prior_arr')::float8 AS prior_arr,
                   (attrs->>'renewed_arr')::float8 AS renewed_arr,
                   (attrs->>'arr_usd')::float8 AS arr_usd
            FROM normalised_events
            WHERE tenant_id = ANY(:tids) AND received_at <= :cutoff
              AND (source, event_type) IN ({values})
            """
        ),
        params,
    ).all()
    columns = [
        "account_id",
        "source",
        "event_type",
        "received_at",
        "stage",
        "renewal_date",
        "prior_arr",
        "renewed_arr",
        "arr_usd",  # Stripe's renewal events carry the ARR here instead
    ]
    df = pd.DataFrame(rows, columns=columns)
    df["received_at"] = utc(df["received_at"])
    df["renewal_date"] = utc(df["renewal_date"])
    return df


def model_version_of(artifact: dict) -> str:
    """``logreg-`` plus the first 12 hex digits of the artifact's SHA-256."""
    canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
    return "logreg-" + hashlib.sha256(canonical.encode()).hexdigest()[:12]


def in_sample_metrics(y: np.ndarray, p: np.ndarray) -> dict:
    """Fit diagnostics on the training rows. Not an evaluation: that is Step 4's holdout."""
    from sklearn.metrics import log_loss, roc_auc_score

    return {
        "rows": int(len(y)),
        "positives": int(y.sum()),
        "positive_rate": float(y.mean()),
        "auc_in_sample": float(roc_auc_score(y, p)),
        "log_loss_in_sample": float(log_loss(y, p)),
    }


def train(cfg: Config, engine: Engine | None = None) -> str:
    """Fit on the config's tenants at ``cfg.train_as_of``, store, activate; return the version."""
    engine = engine if engine is not None else get_engine("app_batch")
    tenant_ids = [t.tenant_id for t in cfg.tenants]
    cutoff = cfg.train_as_of
    with engine.begin() as conn:
        # Two trainers at once would race on which version ends up active.
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(hashtext('app.ml.train'))"))
        anchors = read_anchors(conn, tenant_ids, cutoff)
        contracts = contracts_frame(
            conn.execute(
                sa.text(
                    "SELECT account_id, cycle_number, start_date, end_date, arr"
                    " FROM contracts WHERE tenant_id = ANY(:tids)"
                ),
                {"tids": tenant_ids},
            ).all()
        )
        outcomes = read_outcomes(conn, tenant_ids, cutoff)
        thresholds = {
            r.tenant_id: float(r.contraction_threshold)
            for r in conn.execute(
                sa.text(
                    "SELECT tenant_id, contraction_threshold FROM tenants"
                    " WHERE tenant_id = ANY(:tids)"
                ),
                {"tids": tenant_ids},
            )
        }

        labels = label_anchors(anchors, contracts, outcomes, thresholds, cutoff)
        labelled = anchors[labels.notna()]
        y = labels[labels.notna()].to_numpy(dtype="int64")
        if len(np.unique(y)) < 2:
            raise RuntimeError(
                f"cannot train: {len(y)} labelled anchors with {int(y.sum())} positives"
            )

        scorer = LogisticScorer(FEATURES).fit(labelled, pd.Series(y))
        artifact = scorer.to_artifact()
        version = model_version_of(artifact)
        metrics = in_sample_metrics(y, scorer.score(labelled))
        metrics["censored_rows"] = int(labels.isna().sum())
        row_counts = labelled.groupby("tenant_id").size()

        conn.execute(
            sa.text(
                """
                INSERT INTO model_versions (
                    model_version, kind, feature_set_version, features, artifact, train_as_of,
                    label_cutoff, anchor_first, anchor_last, tenant_row_counts, metrics)
                VALUES (
                    :version, :kind, :fsv, CAST(:features AS jsonb), CAST(:artifact AS jsonb),
                    :train_as_of, :cutoff, :anchor_first, :anchor_last,
                    CAST(:row_counts AS jsonb), CAST(:metrics AS jsonb))
                ON CONFLICT (model_version) DO NOTHING
                """
            ),
            {
                "version": version,
                "kind": scorer.kind,
                "fsv": FEATURE_SET_VERSION,
                "features": json.dumps(list(FEATURES)),
                "artifact": json.dumps(artifact),
                "train_as_of": cfg.train_as_of,
                "cutoff": cutoff,
                "anchor_first": labelled["as_of"].min().to_pydatetime(),
                "anchor_last": labelled["as_of"].max().to_pydatetime(),
                "row_counts": json.dumps({str(k): int(v) for k, v in row_counts.items()}),
                "metrics": json.dumps(metrics),
            },
        )
        # Deactivate first: the partial unique index allows only one active row at any moment.
        conn.execute(
            sa.text(
                "UPDATE model_versions SET is_active = false"
                " WHERE is_active AND model_version <> :version"
            ),
            {"version": version},
        )
        conn.execute(
            sa.text("UPDATE model_versions SET is_active = true WHERE model_version = :version"),
            {"version": version},
        )
    return version
