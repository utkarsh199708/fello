"""Classification metrics shared by training (model_versions.metrics) and ``evaluate``. Pure."""

from __future__ import annotations

import numpy as np


def classification_metrics(y, p) -> dict:
    """Rows, positives, ROC-AUC, PR-AUC (average precision), Brier score and log loss.

    AUCs are None when only one class is present: an undefined number is not printed as 0.5.
    """
    from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score

    y = np.asarray(y, dtype="int64")
    p = np.clip(np.asarray(p, dtype="float64"), 1e-12, 1 - 1e-12)
    both = len(np.unique(y)) == 2
    return {
        "rows": int(len(y)),
        "positives": int(y.sum()),
        "positive_rate": float(y.mean()) if len(y) else None,
        "roc_auc": float(roc_auc_score(y, p)) if both else None,
        "pr_auc": float(average_precision_score(y, p)) if both else None,
        "brier": float(brier_score_loss(y, p)) if len(y) else None,
        "log_loss": float(log_loss(y, p, labels=[0, 1])) if len(y) else None,
    }


def reliability(y, p, bins: int = 10) -> list[dict]:
    """Calibration table: per probability decile, mean predicted against observed rate."""
    y = np.asarray(y, dtype="float64")
    p = np.asarray(p, dtype="float64")
    edges = np.linspace(0.0, 1.0, bins + 1)
    which = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    rows = []
    for b in range(bins):
        mask = which == b
        if mask.any():
            rows.append(
                {
                    "bin": f"{edges[b]:.1f}-{edges[b + 1]:.1f}",
                    "rows": int(mask.sum()),
                    "predicted": float(p[mask].mean()),
                    "observed": float(y[mask].mean()),
                }
            )
    return rows
