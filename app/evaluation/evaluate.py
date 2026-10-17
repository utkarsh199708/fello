"""``python -m app evaluate --config <yaml> [--data data]``: holdout evaluation (tasks 4.6, 4.7).

Everything is measured on the test cycles of the split in ``app.ml.train``: renewals in
``(T, eval_as_of]``, with ``T = train_as_of - 12 months``. The evaluation version, fitted on the
training cycles only, is the model under test.

The headline is **recall@8 per CSM-week**. For each Monday in the holdout and each CSM's book, it
is the share of accounts whose renewal falls within 90 days and is then lost or contracted that
appear in the CSM's top 8 by ``rank_score`` (probability times ARR). Accounts with not enough
signal take no place in the ranking, as on the live list. It is averaged over CSM-weeks that have
such an account, and also reported ARR-weighted and with a ranking by probability alone.

**Lead time.** For every holdout renewal that was lost or contracted: the first Monday the account
entered its CSM's top 8, measured against the notice deadline (30 days before the renewal).

**Five rows on one slice.** Each row is ranked the same way, after a one-feature logistic fit on
the training cycles turns its raw score into a probability. The higher, the riskier.

- **oracle**: minus the latent health from ``data/truth/``, the ceiling;
- **LightGBM** and **logistic regression**: both fitted on the same training rows exactly as
  ``train`` fits them. The one ``train`` chose on the validation tail is the stored evaluation
  version, and is marked "(live kind)";
- **rules**: a count of four hand-written signals, with NULL counted as unknown;
- **days to renewal**: minus days to renewal, the "is this just a calendar" row.

**Leakage probes.**

- AUC by days before renewal. A jump toward 1.0 near the renewal is the signature of an outcome
  leaking into a feature.
- Shuffled-label AUC, which should be near 0.5.
- The AUC of a random row split, printed to show why it is not reported.

These metrics validate the pipeline and the ranking on data whose generating process is known.
They are not evidence about real-world churn.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np
import pandas as pd
import sqlalchemy as sa

from app.config import Config, load_config
from app.db import get_engine
from app.ml.feature_spec import FEATURES
from app.ml.metrics import classification_metrics, reliability
from app.ml.score import MIN_EVENTS, MIN_TENURE_DAYS
from app.ml.scorer import LightGBMScorer, LogisticScorer, scorer_from_artifact
from app.ml.train import (
    TrainingInputs,
    fit_candidates,
    holdout_start,
    labelled,
    read_training_inputs,
    split_validation,
)

TOP_K = 8
WINDOW_DAYS = 90  # a renewal within this many days of the anchor is "due"
NOTICE_DAYS = 30
SIGNAL_FEATURES = {  # what a planted case's top reason should be
    "tickets_30d",
    "tickets_90d",
    "ticket_rate_30d_vs_prior_90d",
    "open_tickets",
    "high_priority_30d",
    "days_since_last_ticket",
    "active_users_last_month",
    "sessions_last_month",
    "usage_change_last_month_vs_prior_3",
    "seat_utilisation",
    "months_since_last_activity",
}
LEAD_PROBES = ((90, 84, 96), (60, 54, 66), (30, 24, 36), (7, 0, 6))  # name, lo, hi (days)


# --- raw scores of each method ----------------------------------------------------------------


def rules_score(X: pd.DataFrame) -> np.ndarray:
    """Four signals a CSM lead would write down. NULL never fires a signal (unknown, not bad)."""
    signals = [
        X["ticket_rate_30d_vs_prior_90d"] >= 2.0,
        X["usage_change_last_month_vs_prior_3"] <= -0.3,
        X["failed_payments_90d"] >= 1,
        X["days_since_last_inbound"] >= 60,
    ]
    return np.sum([s.fillna(False).to_numpy(dtype=bool) for s in signals], axis=0).astype(float)


def calibrate(raw_train: np.ndarray, y_train, raw: np.ndarray) -> np.ndarray:
    """A one-feature logistic fit on the training cycles: raw score to probability."""
    from sklearn.linear_model import LogisticRegression

    model = LogisticRegression(max_iter=1000)
    model.fit(np.asarray(raw_train, dtype=float).reshape(-1, 1), np.asarray(y_train, dtype=int))
    return model.predict_proba(np.asarray(raw, dtype=float).reshape(-1, 1))[:, 1]


def read_health(data_dir: Path) -> dict[tuple[str, str], float]:
    """Latent monthly health per (account_id, YYYY-MM), from data/truth/."""
    with (data_dir / "truth" / "health_monthly.csv").open(encoding="utf-8", newline="") as fh:
        return {(r["account_id"], r["month"]): float(r["health"]) for r in csv.DictReader(fh)}


def oracle_raw(frame: pd.DataFrame, health: dict) -> np.ndarray:
    """Minus the latent health of the month before each anchor. Higher is riskier."""
    months = (frame["as_of"] - pd.Timedelta(days=1)).dt.strftime("%Y-%m")
    values = [
        health.get((str(a), m), np.nan) for a, m in zip(frame["account_id"], months, strict=True)
    ]
    out = -np.asarray(values, dtype=float)
    return np.where(np.isnan(out), np.nanmedian(out), out)


# --- ranking metrics ------------------------------------------------------------------------


def rank_in_books(frame: pd.DataFrame, by: str) -> pd.Series:
    """Rank within (tenant, CSM, Monday) by ``by`` descending; NaN for not-enough-signal rows."""
    ranked = frame[frame["scored"]].sort_values(by, ascending=False, kind="stable")
    ranks = ranked.groupby(["tenant_id", "csm_id", "as_of"], dropna=False).cumcount() + 1
    return ranks.reindex(frame.index)


def recall_at_k(frame: pd.DataFrame, rank: pd.Series, weeks) -> dict:
    """recall@8 per CSM-week (plain and ARR-weighted) and precision@8 with its ceiling."""
    f = frame[frame["as_of"].isin(weeks)].assign(top=rank.reindex(frame.index) <= TOP_K)
    f = f.assign(hit=f["due_positive"] & f["top"])
    groups = f.groupby(["tenant_id", "csm_id", "as_of"], dropna=False)
    summary = groups.agg(
        positives=("due_positive", "sum"),
        hits=("hit", "sum"),
        arr_positive=("arr_due_positive", "sum"),
        arr_hit=("arr_hit", "sum") if "arr_hit" in f else ("hit", "sum"),
    )
    hit_arr = (
        (f["arr"] * f["hit"]).groupby([f["tenant_id"], f["csm_id"], f["as_of"]], dropna=False).sum()
    )
    summary["arr_hit"] = hit_arr.reindex(summary.index).fillna(0.0)
    with_pos = summary[summary["positives"] > 0]
    if with_pos.empty:
        return {"csm_weeks": 0}
    return {
        "csm_weeks": int(len(with_pos)),
        "recall_at_8": float((with_pos["hits"] / with_pos["positives"]).mean()),
        "arr_recall_at_8": float(with_pos["arr_hit"].sum() / with_pos["arr_positive"].sum()),
        "precision_at_8": float((with_pos["hits"] / TOP_K).mean()),
        "precision_ceiling": float((with_pos["positives"].clip(upper=TOP_K) / TOP_K).mean()),
    }


def lead_times(frame: pd.DataFrame, rank: pd.Series, lost: pd.DataFrame) -> dict:
    """First top-8 Monday of each lost holdout renewal, against its notice deadline."""
    f = frame.assign(top=rank.reindex(frame.index) <= TOP_K)
    entries = []
    for r in lost.itertuples():
        deadline = r.renewal_date - pd.Timedelta(days=NOTICE_DAYS)
        weeks = f[
            (f["account_id"] == r.account_id)
            & (f["as_of"] > r.renewal_date - pd.Timedelta(days=365))
            & (f["as_of"] < r.renewal_date)
            & f["top"]
        ]
        first = weeks["as_of"].min() if len(weeks) else pd.NaT
        lead = (deadline - first).days if pd.notna(first) else None
        entries.append({"account_id": r.account_id, "renewal_date": r.renewal_date, "lead": lead})
    leads = [e["lead"] for e in entries]
    n = len(leads)
    before = [x for x in leads if x is not None and x >= 0]
    return {
        "renewals": n,
        "caught_30d_before_notice": sum(x is not None and x >= 30 for x in leads) / n
        if n
        else None,
        "caught_60d_before_notice": sum(x is not None and x >= 60 for x in leads) / n
        if n
        else None,
        "never_in_top_8": sum(x is None for x in leads),
        "median_lead_days": float(np.median(before)) if before else None,
        "entries": entries,
    }


# --- the evaluation -------------------------------------------------------------------------


def auc_difference_ci(y, p_a, p_b, groups, reps: int = 200, seed: int = 7) -> dict:
    """Paired bootstrap over accounts of ROC-AUC(a) - ROC-AUC(b), with a 95% interval.

    Accounts are resampled (Poisson weights per account), not rows: one account's weekly rows
    are near-copies, and resampling them one by one would make the interval far too narrow.
    """
    from sklearn.metrics import roc_auc_score

    y = np.asarray(y)
    codes, uniques = pd.factorize(pd.Series(groups).astype(str))
    rng = np.random.default_rng(seed)
    diffs = []
    for _ in range(reps):
        w = rng.poisson(1.0, len(uniques))[codes].astype(float)
        if w.sum() == 0 or len(np.unique(y[w > 0])) < 2:
            continue
        diffs.append(
            roc_auc_score(y, p_a, sample_weight=w) - roc_auc_score(y, p_b, sample_weight=w)
        )
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    point = roc_auc_score(y, p_a) - roc_auc_score(y, p_b)
    return {"difference": float(point), "ci95": [float(lo), float(hi)], "reps": len(diffs)}


def evaluation_model(conn):
    row = conn.execute(
        sa.text(
            "SELECT e.model_version, e.kind, e.artifact FROM model_versions l"
            " JOIN model_versions e ON e.model_version = l.validated_by WHERE l.is_active"
        )
    ).one_or_none()
    if row is None:
        raise LookupError(
            "the active model has no evaluation version (history too short for a holdout?)"
        )
    return row.model_version, scorer_from_artifact(row.kind, row.artifact)


def anchor_frame(conn, cfg: Config, inputs: TrainingInputs) -> pd.DataFrame:
    """Every anchor with its CSM, renewal, label (as known at eval_as_of) and evidence state."""
    tenant_ids = [t.tenant_id for t in cfg.tenants]
    csm = {
        r.account_id: r.csm_id
        for r in conn.execute(
            sa.text("SELECT account_id, csm_id FROM accounts WHERE tenant_id = ANY(:t)"),
            {"t": tenant_ids},
        )
    }
    from app.ml.labels import label_frame

    lf = label_frame(
        inputs.anchors, inputs.contracts, inputs.outcomes, inputs.thresholds, cfg.eval_as_of
    )
    f = inputs.anchors.copy()
    f["label"] = lf["label"]
    f["renewal_date"] = lf["renewal_date"]
    f["csm_id"] = f["account_id"].map(csm)
    f["scored"] = (f["account_tenure_days"] >= MIN_TENURE_DAYS) & (
        f["total_event_count"] >= MIN_EVENTS
    )
    due = (f["renewal_date"] - f["as_of"]).dt.days <= WINDOW_DAYS
    f["due_positive"] = due & (f["label"] == 1)
    f["arr_due_positive"] = f["arr"] * f["due_positive"]
    return f.reset_index(drop=True)


def evaluate(cfg: Config, data_dir: str | Path) -> dict:
    data_dir = Path(data_dir)
    T = holdout_start(cfg)
    with get_engine("app_batch").connect() as conn:
        version, stored = evaluation_model(conn)
        inputs = read_training_inputs(conn, cfg)
        frame = anchor_frame(conn, cfg, inputs)
    train_rows = labelled(inputs, T, renew_upto=T)
    test_rows = labelled(inputs, cfg.eval_as_of, renew_after=T, renew_upto=cfg.eval_as_of)
    health = read_health(data_dir)

    # Both candidates, fitted exactly as train fits them; the stored one is used as is.
    fit_rows, valid_rows = split_validation(train_rows)
    models = fit_candidates(fit_rows, valid_rows)
    models[stored.kind] = stored
    gbm, logreg = models[LightGBMScorer.kind], models[LogisticScorer.kind]
    live_name = "lightgbm" if stored.kind == LightGBMScorer.kind else "logistic regression"
    live = stored
    # Raw scores on every anchor (for rankings) and on the train rows (for calibration).
    raw = {
        "oracle": (oracle_raw(train_rows, health), oracle_raw(frame, health)),
        "lightgbm": (gbm.margin(train_rows), gbm.margin(frame)),
        "logistic regression": (logreg.margin(train_rows), logreg.margin(frame)),
        "rules": (rules_score(train_rows), rules_score(frame)),
        "days to renewal": (
            -train_rows["days_to_renewal"].to_numpy(float),
            -frame["days_to_renewal"].to_numpy(float),
        ),
    }
    prob = {name: calibrate(tr, train_rows["label"], fr) for name, (tr, fr) in raw.items()}
    # The two models keep their own probabilities; only the baselines need calibrating.
    prob["lightgbm"] = gbm.score(frame)
    prob["logistic regression"] = logreg.score(frame)

    key = ["tenant_id", "account_id", "as_of"]
    test_idx = frame.reset_index().merge(test_rows[key], on=key)["index"].to_numpy()
    y_test = frame.loc[test_idx, "label"].astype(int).to_numpy()
    weeks = sorted(
        w
        for w in frame["as_of"].unique()
        if T < w <= pd.Timestamp(cfg.eval_as_of) - pd.Timedelta(days=WINDOW_DAYS)
    )
    lost = (
        test_rows[test_rows["label"] == 1][["account_id", "renewal_date"]]
        .drop_duplicates()
        .sort_values(["renewal_date", "account_id"])
    )

    comparison, ranks = {}, {}
    for name, p in prob.items():
        frame[f"rank_score:{name}"] = p * frame["arr"]
        ranks[name] = rank_in_books(frame, f"rank_score:{name}")
        lead = lead_times(frame, ranks[name], lost)
        comparison[name] = {
            **classification_metrics(y_test, p[test_idx]),
            **recall_at_k(frame, ranks[name], weeks),
            **{k: v for k, v in lead.items() if k != "entries"},
        }
    frame["p:live"] = prob[live_name]
    lgbm_vs_lr = auc_difference_ci(
        y_test,
        prob[live_name][test_idx],
        prob["logistic regression"][test_idx],
        frame.loc[test_idx, "account_id"],
    )
    by_probability = recall_at_k(frame, rank_in_books(frame, "p:live"), weeks)

    # Slices of the model under test.
    test = frame.loc[test_idx].assign(p=prob[live_name][test_idx])
    slugs = {str(t.tenant_id): t.slug for t in cfg.tenants}
    slices: dict[str, dict] = {"tenant": {}, "days_to_renewal": {}, "event_count": {}}
    for t in cfg.tenants:
        part = test[test["tenant_id"].astype(str) == str(t.tenant_id)]
        slices["tenant"][t.slug] = (
            classification_metrics(part["label"], part["p"])
            if len(part)
            else "no observed renewals"
        )
    for name, lo, hi in (("0-90", -10_000, 90), ("91-180", 91, 180), ("over 180", 181, 10_000)):
        part = test[test["days_to_renewal"].between(lo, hi)]
        slices["days_to_renewal"][name] = classification_metrics(part["label"], part["p"])
    for name, lo, hi in (("under 10", 0, 9), ("10-100", 10, 100), ("over 100", 101, 10**9)):
        part = test[test["total_event_count"].between(lo, hi)]
        slices["event_count"][name] = classification_metrics(part["label"], part["p"])

    # Is this a calendar? Share of gain and of top reasons that is days_to_renewal.
    _, contrib = live.contributions(test)
    top_reason = contrib.abs().idxmax(axis=1)
    share = contrib.abs().mean() / contrib.abs().mean().sum()
    calendar = {
        "contribution_share": float(share["days_to_renewal"]),
        "top_reason_share": float((top_reason == "days_to_renewal").mean()),
    }

    # Planted cases: caught in time, and for the right reason.
    planted = []
    path = data_dir / "truth" / "planted_cases.csv"
    with path.open(encoding="utf-8", newline="") as fh:
        cases = list(csv.DictReader(fh))
    entries = {
        str(e["account_id"]): e["lead"]
        for e in lead_times(frame, ranks[live_name], lost)["entries"]
    }
    for case in cases:
        rows = frame[
            (frame["account_id"].astype(str) == case["account_id"])
            & (frame["as_of"] >= pd.Timestamp(case["signal_start"], tz="UTC"))
            & (frame["as_of"] < pd.Timestamp(case["renewal_date"], tz="UTC"))
        ]
        reason = None
        if len(rows):
            _, c = live.contributions(rows.tail(1))
            reason = str(c.iloc[0].idxmax())
        planted.append(
            {
                "account_id": case["account_id"],
                "renewal_date": case["renewal_date"],
                "lead_days": entries.get(case["account_id"]),
                "top_reason": reason,
                "reason_matches": reason in SIGNAL_FEATURES,
            }
        )

    # Worst misses: lost renewals that never reached the top 8, largest ARR first.
    lead = lead_times(frame, ranks[live_name], lost)
    names = {}
    with get_engine("app_batch").connect() as conn:
        for r in conn.execute(sa.text("SELECT account_id, name, tenant_id FROM accounts")):
            names[str(r.account_id)] = (r.name, slugs.get(str(r.tenant_id), "?"))
    arr_of = test.groupby(test["account_id"].astype(str))["arr"].max()
    misses = sorted(
        (
            {
                "account": names.get(str(e["account_id"]), ("?", "?"))[0],
                "tenant": names.get(str(e["account_id"]), ("?", "?"))[1],
                "arr": float(arr_of.get(str(e["account_id"]), 0.0)),
                "renewal_date": e["renewal_date"].date().isoformat(),
            }
            for e in lead["entries"]
            if e["lead"] is None
        ),
        key=lambda m: -m["arr"],
    )

    probes = leakage_probes(train_rows, test_rows, live, inputs, cfg)
    return {
        "model_version": version,
        "live_kind": live_name,
        "split": {
            "holdout_start": T.date().isoformat(),
            "eval_as_of": cfg.eval_as_of.date().isoformat(),
            "train_rows": int(len(train_rows)),
            "test_rows": int(len(test_rows)),
            "test_positives": int(test_rows["label"].sum()),
            "holdout_weeks": len(weeks),
            "lost_renewals": int(len(lost)),
        },
        "comparison": comparison,
        "lightgbm_by_probability": by_probability,
        "lightgbm_minus_logreg_roc_auc": lgbm_vs_lr,
        "reliability": reliability(y_test, prob[live_name][test_idx]),
        "slices": slices,
        "calendar": calendar,
        "planted": planted,
        "worst_misses": misses[:10],
        "worst_misses_total": len(misses),
        "leakage": probes,
    }


def leakage_probes(train_rows, test_rows, model, inputs, cfg) -> dict:
    from sklearn.metrics import roc_auc_score

    p = model.score(test_rows)
    by_lead = {}
    for name, lo, hi in LEAD_PROBES:
        part = test_rows["days_to_renewal"].between(lo, hi).to_numpy()
        y = test_rows["label"].to_numpy()[part]
        by_lead[str(name)] = float(roc_auc_score(y, p[part])) if len(np.unique(y)) == 2 else None

    shuffled = train_rows["label"].sample(frac=1.0, random_state=7).to_numpy()
    lr = LogisticScorer(FEATURES).fit(train_rows, pd.Series(shuffled))
    shuffled_auc = float(roc_auc_score(test_rows["label"], lr.score(test_rows)))

    everything = labelled(inputs, cfg.train_as_of, renew_upto=cfg.train_as_of)
    rng = np.random.default_rng(7)
    mask = rng.random(len(everything)) < 0.8
    if isinstance(model, LightGBMScorer):
        rand = LightGBMScorer(FEATURES, num_boost_round=model.num_boost_round)
    else:
        rand = LogisticScorer(FEATURES)
    rand = rand.fit(everything[mask], everything["label"][mask])
    random_auc = float(roc_auc_score(everything["label"][~mask], rand.score(everything[~mask])))
    values = [v for v in by_lead.values() if v is not None]
    jump = (max(values) - by_lead["90"]) if by_lead.get("90") is not None and values else None
    return {
        "auc_by_days_before_renewal": by_lead,
        "short_lead_jump": jump,
        "short_lead_ok": jump is None or not (max(values) > 0.95 and jump > 0.15),
        "shuffled_label_auc": shuffled_auc,
        "shuffled_ok": abs(shuffled_auc - 0.5) < 0.05,
        "random_split_auc_not_reported": random_auc,
    }


# --- report ----------------------------------------------------------------------------------


def _f(value, digits=3, pct=False) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "-"
    return f"{value:.1%}" if pct else f"{value:.{digits}f}"


def render(r: dict) -> str:
    s = r["split"]
    out = [
        f"Model under test: {r['model_version']}, the evaluation fit of the kind chosen on the"
        f" validation tail ({r['live_kind']}). The live version is its refit on every cycle.",
        f"Training cycles renew on or before {s['holdout_start']}; test cycles renew after it, up"
        f" to {s['eval_as_of']}.",
        f"{s['train_rows']:,} training rows; {s['test_rows']:,} test rows with"
        f" {s['test_positives']:,} positives; {s['holdout_weeks']} holdout Mondays;"
        f" {s['lost_renewals']} renewals lost or contracted.",
        "",
        "| Method | PR-AUC | ROC-AUC | Brier | recall@8 | ARR recall@8 | precision@8 |"
        " caught 30d before notice | caught 60d | median lead (days) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, m in r["comparison"].items():
        label = f"{name} (live kind)" if name == r["live_kind"] else name
        out.append(
            f"| {label} | {_f(m.get('pr_auc'))} | {_f(m.get('roc_auc'))} | {_f(m.get('brier'))} |"
            f" {_f(m.get('recall_at_8'), pct=True)} | {_f(m.get('arr_recall_at_8'), pct=True)} |"
            f" {_f(m.get('precision_at_8'), pct=True)} |"
            f" {_f(m.get('caught_30d_before_notice'), pct=True)} |"
            f" {_f(m.get('caught_60d_before_notice'), pct=True)} |"
            f" {_f(m.get('median_lead_days'), 0)} |"
        )
    ceiling = next(iter(r["comparison"].values())).get("precision_ceiling")
    bp = r["lightgbm_by_probability"]
    d = r["lightgbm_minus_logreg_roc_auc"]
    out += [
        "",
        f"precision@8 ceiling (if every due loss were in the top 8): {_f(ceiling, pct=True)}.",
        f"The live kind ranked by probability alone instead of probability x ARR:"
        f" recall@8 {_f(bp.get('recall_at_8'), pct=True)}, ARR recall@8"
        f" {_f(bp.get('arr_recall_at_8'), pct=True)}.",
        f"LightGBM minus logistic regression, ROC-AUC: {d['difference']:+.3f}, 95% interval"
        f" {d['ci95'][0]:+.3f} to {d['ci95'][1]:+.3f} (paired bootstrap over accounts)"
        + (": within noise." if d["ci95"][0] <= 0 <= d["ci95"][1] else "."),
        "",
        f"Reliability ({r['live_kind']}, test rows):",
        "",
        "| Predicted bin | Rows | Mean predicted | Observed |",
        "|---|---|---|---|",
    ]
    out += [
        f"| {b['bin']} | {b['rows']:,} | {_f(b['predicted'])} | {_f(b['observed'])} |"
        for b in r["reliability"]
    ]
    out += [
        "",
        f"Slices ({r['live_kind']}, test rows):",
        "",
        "| Slice | Rows | Positives | PR-AUC | ROC-AUC |",
        "|---|---|---|---|---|",
    ]
    for kind, parts in r["slices"].items():
        for name, m in parts.items():
            if isinstance(m, str):
                out.append(f"| {kind} {name} | - | - | {m} | |")
            else:
                out.append(
                    f"| {kind} {name} | {m['rows']:,} | {m['positives']:,} | {_f(m['pr_auc'])} |"
                    f" {_f(m['roc_auc'])} |"
                )
    c = r["calendar"]
    planted = r["planted"]
    caught = sum(p["lead_days"] is not None and p["lead_days"] >= 28 for p in planted)
    matched = sum(p["reason_matches"] for p in planted)
    lk = r["leakage"]
    out += [
        "",
        f"Is it a calendar? days_to_renewal carries {_f(c['contribution_share'], pct=True)} of the"
        f" absolute contributions and is the top reason on {_f(c['top_reason_share'], pct=True)}"
        " of test rows.",
        f"Planted cases: {caught} of {len(planted)} entered their CSM's top 8 at least 4 weeks"
        f" before the notice deadline; the top reason was a ticket or usage feature for"
        f" {matched} of {len(planted)}.",
        f"Worst misses: {r['worst_misses_total']} renewals lost or contracted never reached their"
        " CSM's top 8." + (" Largest:" if r["worst_misses"] else ""),
    ]
    out += [
        f"- {m['tenant']} {m['account']}, ARR {m['arr']:,.0f}, renewal {m['renewal_date']}"
        for m in r["worst_misses"][:5]
    ]
    auc = lk["auc_by_days_before_renewal"]
    out += [
        "",
        "Leakage probes:",
        "",
        "- AUC by days before renewal: "
        + ", ".join(f"{k} days {_f(v)}" for k, v in auc.items())
        + (" (no jump toward 1.0: ok)" if lk["short_lead_ok"] else " (JUMP: check for leakage)"),
        f"- Shuffled-label AUC: {_f(lk['shuffled_label_auc'])}"
        + (" (near 0.5: ok)" if lk["shuffled_ok"] else " (NOT near 0.5)"),
        f"- Random row split AUC: {_f(lk['random_split_auc_not_reported'])}, not reported: rows of"
        " one account would sit on both sides.",
    ]
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app evaluate")
    parser.add_argument("--config", required=True)
    parser.add_argument("--data", default="data")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args(argv[1:])
    result = evaluate(load_config(args.config), args.data)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(render(result), end="")
    ok = result["leakage"]["short_lead_ok"] and result["leakage"]["shuffled_ok"]
    return 0 if ok else 1
