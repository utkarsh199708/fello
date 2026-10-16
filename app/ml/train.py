"""Train the one global model: an evaluation fit and a live refit, both in ``model_versions``.

Training is the single deliberate cross-tenant reader (docs/PLAN.md section 8), so it connects as
``app_batch``, the role with BYPASSRLS, and reads only the tenants named in the config. It can
read every tenant table but write only ``model_versions``.

The split is by renewal cycle and by time (task 4.2). Every weekly anchor is labelled with the
outcome of its next renewal, and goes to the side of that renewal's date:

- **Cut-off** ``T = train_as_of - 12 months``.
- **Training cycles** renew on or before ``T``. Their labels use only outcomes received by
  ``T``. The time-ordered last ``VALID_SHARE`` of those cycles is the early-stopping set.
- **Test cycles** renew in ``(T, eval_as_of]``, labelled with what was received by
  ``eval_as_of``. Every test outcome comes after every training outcome, and an account's
  near-identical weekly rows never straddle the split.

Model choice is a rule, decided on the validation tail and never on the test cycles. LightGBM
(early-stopped on the tail) and logistic regression are both fitted on the training cycles
before the tail. LightGBM, the planned model, is kept unless logistic regression is clearly
better: at least ``SWITCH_MARGIN`` lower log loss on a tail of at least
``MIN_SELECTION_CYCLES`` renewal cycles. Log loss is used because ``rank_score`` multiplies the
probability by ARR, so calibration matters as much as order. A smaller tail is too noisy to
choose on: on config/small.yaml (22 cycles) logistic regression won the tail and then failed the
test cycles. Every measured value is stored, so the choice can be checked.

Two versions are stored (task 4.4):

1. **evaluation**: the chosen model fit on the training cycles and scored on the test cycles.
   Its metrics are the honest ones.
2. **live**: the chosen kind refit on every cycle observed by ``train_as_of`` with the same
   settings (for LightGBM, the number of rounds the evaluation fit stopped at). ``validated_by``
   names the evaluation version. Only this one is activated and scores.

When the history is too short for a holdout (a test fixture), the evaluation fit is skipped and
said so in the live version's metrics. Versions are hashes of the fitted artifact, so the same
data gives the same versions, and storing them again only re-activates the live one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.config import Config
from app.db import get_engine
from app.ml.feature_spec import FEATURE_SET_VERSION, FEATURES
from app.ml.features import contracts_frame, utc
from app.ml.labels import label_frame
from app.ml.metrics import classification_metrics
from app.ml.scorer import LightGBMScorer, LogisticScorer, Scorer
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


# The holdout is the last 12 months of observed renewals; the early-stopping set is the latest
# 15% of the training cycles; a split with fewer positives than this is too small to use.
HOLDOUT_MONTHS = 12
VALID_SHARE = 0.15
MIN_POSITIVES = 10
# Model choice: switch away from LightGBM only for a clear win on enough validation cycles.
SWITCH_MARGIN = 0.01  # relative log loss
MIN_SELECTION_CYCLES = 100


def model_version_of(kind: str, artifact: dict) -> str:
    """The scorer kind plus the first 12 hex digits of the artifact's SHA-256."""
    canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
    return f"{kind}-" + hashlib.sha256(canonical.encode()).hexdigest()[:12]


def holdout_start(cfg: Config) -> pd.Timestamp:
    """``T``: renewals after it, up to ``eval_as_of``, are test cycles."""
    return pd.Timestamp(cfg.train_as_of) - pd.DateOffset(months=HOLDOUT_MONTHS)


@dataclass
class TrainingInputs:
    anchors: pd.DataFrame
    contracts: pd.DataFrame
    outcomes: pd.DataFrame
    thresholds: dict


def read_training_inputs(conn: Connection, cfg: Config) -> TrainingInputs:
    tenant_ids = [t.tenant_id for t in cfg.tenants]
    upto = max(cfg.train_as_of, cfg.eval_as_of)
    contracts = contracts_frame(
        conn.execute(
            sa.text(
                "SELECT account_id, cycle_number, start_date, end_date, arr"
                " FROM contracts WHERE tenant_id = ANY(:tids)"
            ),
            {"tids": tenant_ids},
        ).all()
    )
    thresholds = {
        r.tenant_id: float(r.contraction_threshold)
        for r in conn.execute(
            sa.text(
                "SELECT tenant_id, contraction_threshold FROM tenants WHERE tenant_id = ANY(:tids)"
            ),
            {"tids": tenant_ids},
        )
    }
    return TrainingInputs(
        anchors=read_anchors(conn, tenant_ids, cfg.train_as_of),
        contracts=contracts,
        outcomes=read_outcomes(conn, tenant_ids, upto),
        thresholds=thresholds,
    )


def labelled(inputs: TrainingInputs, cutoff, renew_after=None, renew_upto=None) -> pd.DataFrame:
    """Anchors with a known label at ``cutoff`` whose renewal falls in (renew_after, renew_upto]."""
    lf = label_frame(inputs.anchors, inputs.contracts, inputs.outcomes, inputs.thresholds, cutoff)
    keep = lf["label"].notna()
    if renew_after is not None:
        keep &= lf["renewal_date"] > pd.Timestamp(renew_after)
    if renew_upto is not None:
        keep &= lf["renewal_date"] <= pd.Timestamp(renew_upto)
    out = inputs.anchors[keep].copy()
    out["label"] = lf.loc[keep, "label"].astype("int64")
    out["renewal_date"] = lf.loc[keep, "renewal_date"]
    return out.sort_values(["renewal_date", "tenant_id", "account_id", "as_of"], kind="stable")


def enough(rows: pd.DataFrame) -> bool:
    return len(rows) > 0 and MIN_POSITIVES <= int(rows["label"].sum()) < len(rows)


def split_validation(rows: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The latest ``VALID_SHARE`` of cycles (by renewal date) for early stopping, whole cycles."""
    dates = np.sort(rows["renewal_date"].unique())
    cut = dates[int(len(dates) * (1 - VALID_SHARE))] if len(dates) > 1 else dates[-1]
    return rows[rows["renewal_date"] < cut], rows[rows["renewal_date"] >= cut]


def fit_candidates(fit_rows: pd.DataFrame, valid_rows: pd.DataFrame) -> dict[str, Scorer]:
    """Both candidate models on the same rows. LightGBM stops early on the validation tail."""
    return {
        LightGBMScorer.kind: LightGBMScorer(FEATURES).fit(
            fit_rows, fit_rows["label"], valid_rows, valid_rows["label"]
        ),
        LogisticScorer.kind: LogisticScorer(FEATURES).fit(fit_rows, fit_rows["label"]),
    }


def choose(validation_log_loss: dict[str, float], cycles: int) -> str:
    """LightGBM, unless logistic regression is clearly better on enough validation cycles."""
    default, challenger = LightGBMScorer.kind, LogisticScorer.kind
    clearly_better = validation_log_loss[challenger] < validation_log_loss[default] * (
        1 - SWITCH_MARGIN
    )
    return challenger if cycles >= MIN_SELECTION_CYCLES and clearly_better else default


def store(
    conn: Connection,
    scorer: Scorer,
    *,
    role: str,
    rows: pd.DataFrame,
    train_as_of,
    label_cutoff,
    metrics: dict,
    validated_by: str | None = None,
) -> str:
    artifact = scorer.to_artifact()
    version = model_version_of(scorer.kind, artifact)
    row_counts = rows.groupby("tenant_id").size()
    conn.execute(
        sa.text(
            """
            INSERT INTO model_versions (
                model_version, kind, feature_set_version, features, artifact, train_as_of,
                label_cutoff, anchor_first, anchor_last, tenant_row_counts, metrics, role,
                validated_by)
            VALUES (
                :version, :kind, :fsv, CAST(:features AS jsonb), CAST(:artifact AS jsonb),
                :train_as_of, :cutoff, :anchor_first, :anchor_last,
                CAST(:row_counts AS jsonb), CAST(:metrics AS jsonb), :role, :validated_by)
            ON CONFLICT (model_version) DO NOTHING
            """
        ),
        {
            "version": version,
            "kind": scorer.kind,
            "fsv": FEATURE_SET_VERSION,
            "features": json.dumps(list(FEATURES)),
            "artifact": json.dumps(artifact),
            "train_as_of": pd.Timestamp(train_as_of).to_pydatetime(),
            "cutoff": pd.Timestamp(label_cutoff).to_pydatetime(),
            "anchor_first": rows["as_of"].min().to_pydatetime(),
            "anchor_last": rows["as_of"].max().to_pydatetime(),
            "row_counts": json.dumps({str(k): int(v) for k, v in row_counts.items()}),
            "metrics": json.dumps(metrics),
            "role": role,
            "validated_by": validated_by,
        },
    )
    return version


def train(cfg: Config, engine: Engine | None = None) -> str:
    """Fit the evaluation and live versions, store both, activate the live one; return it."""
    engine = engine if engine is not None else get_engine("app_batch")
    T = holdout_start(cfg)
    with engine.begin() as conn:
        # Two trainers at once would race on which version ends up active.
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(hashtext('app.ml.train'))"))
        inputs = read_training_inputs(conn, cfg)

        train_rows = labelled(inputs, T, renew_upto=T)
        test_rows = labelled(inputs, cfg.eval_as_of, renew_after=T, renew_upto=cfg.eval_as_of)
        evaluation, rounds, kind, selection = None, None, LightGBMScorer.kind, None
        split = {
            "holdout_start": T.isoformat(),
            "eval_as_of": cfg.eval_as_of.isoformat(),
            "train_rows": int(len(train_rows)),
            "test_rows": int(len(test_rows)),
        }
        if enough(train_rows) and enough(test_rows):
            fit_rows, valid_rows = split_validation(train_rows)
            candidates = fit_candidates(fit_rows, valid_rows)
            cycles = int(valid_rows.groupby(["account_id", "renewal_date"]).ngroups)
            measured = {
                name: classification_metrics(valid_rows["label"], s.score(valid_rows))
                for name, s in candidates.items()
            }
            kind = choose({name: m["log_loss"] for name, m in measured.items()}, cycles)
            selection = {
                "validation_cycles": cycles,
                "validation_log_loss": {n: m["log_loss"] for n, m in measured.items()},
                "validation_roc_auc": {n: m["roc_auc"] for n, m in measured.items()},
                "chosen": kind,
            }
            scorer = candidates[kind]
            rounds = getattr(scorer, "num_boost_round", None)
            metrics = {
                "split": {**split, "valid_rows": int(len(valid_rows))},
                "selection": selection,
                "holdout": classification_metrics(test_rows["label"], scorer.score(test_rows)),
                "num_boost_round": rounds,
                "num_threads": LightGBMScorer.NUM_THREADS,
            }
            if isinstance(scorer, LightGBMScorer):
                metrics["importance_gain_share"] = scorer.importance()
            evaluation = store(
                conn,
                scorer,
                role="evaluation",
                rows=train_rows,
                train_as_of=T,
                label_cutoff=T,
                metrics=metrics,
            )

        live_rows = labelled(inputs, cfg.train_as_of, renew_upto=cfg.train_as_of)
        if len(live_rows["label"].unique()) < 2:
            raise RuntimeError(
                f"cannot train: {len(live_rows)} labelled anchors"
                f" with {int(live_rows['label'].sum())} positives"
            )
        if kind == LightGBMScorer.kind:
            live = LightGBMScorer(FEATURES, num_boost_round=rounds)
        else:
            live = LogisticScorer(FEATURES)
        live = live.fit(live_rows, live_rows["label"])
        metrics = {
            "in_sample": classification_metrics(live_rows["label"], live.score(live_rows)),
            "num_boost_round": getattr(live, "num_boost_round", None),
            "num_threads": LightGBMScorer.NUM_THREADS,
            "selection": selection,
            "evaluation": evaluation or "skipped: not enough history for a holdout",
            "split": split,
            "censored_rows": int(len(inputs.anchors) - len(live_rows)),
        }
        version = store(
            conn,
            live,
            role="live",
            rows=live_rows,
            train_as_of=cfg.train_as_of,
            label_cutoff=cfg.train_as_of,
            metrics=metrics,
            validated_by=evaluation,
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
