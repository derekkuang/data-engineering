"""Shared scoring for probabilistic forecasts: proper scoring rules + calibration.

Both the benchmark EDA and the walk-forward model report measure "the bar" and
"the model" through these functions, so the comparison is always apples-to-apples.

Why proper scoring rules over accuracy: directional accuracy collapses a 51/49
call and a 95/5 call into the same "correct". Log loss and Brier reward
*calibrated probability* — exactly what a cost-aware bettor needs. The reliability
table shows WHERE the probabilities are trustworthy, summarised as ECE (expected
calibration error: how far stated probability drifts from observed frequency,
weighted by how many windows fall in each bin).
"""

import numpy as np
import numpy.typing as npt
import pandas as pd
from sklearn.metrics import brier_score_loss, log_loss

EPS = 1e-6  # clip probs off the 0/1 boundary so log loss stays finite
N_BINS = 10


def reliability_table(y: npt.ArrayLike, p: npt.ArrayLike, n_bins: int = N_BINS) -> pd.DataFrame:
    """Bin predictions into equal-width [0,0.1)..[0.9,1.0] buckets and compare the
    mean predicted probability against the observed frequency in each. A calibrated
    forecaster has pred_mean ~= obs_freq in every populated bin."""
    y_arr = np.asarray(y, dtype=int)
    p_arr = np.asarray(p, dtype=float)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bin_idx = np.clip(np.digitize(p_arr, edges) - 1, 0, n_bins - 1)

    rows = []
    for b in range(n_bins):
        mask = bin_idx == b
        count = int(mask.sum())
        if count == 0:
            continue
        rows.append(
            {
                "bin": f"[{edges[b]:.1f},{edges[b + 1]:.1f})",
                "n": count,
                "pred_mean": float(p_arr[mask].mean()),
                "obs_freq": float(y_arr[mask].mean()),
                "gap": float(p_arr[mask].mean() - y_arr[mask].mean()),
            }
        )
    return pd.DataFrame(rows)


def score(y: npt.ArrayLike, p: npt.ArrayLike) -> dict[str, float]:
    """Proper scoring rules + calibration for a probabilistic forecast.

    Returns n, log_loss, brier, accuracy (@0.5), and ece. Lower is better for
    log_loss / brier / ece; higher is better for accuracy.
    """
    y_arr = np.asarray(y, dtype=int)
    p_arr = np.clip(np.asarray(p, dtype=float), EPS, 1.0 - EPS)
    n = int(len(y_arr))

    table = reliability_table(y_arr, p_arr)
    ece = float((table["n"] * table["gap"].abs()).sum() / n) if n else float("nan")

    return {
        "n": float(n),
        "log_loss": float(log_loss(y_arr, p_arr)),
        "brier": float(brier_score_loss(y_arr, p_arr)),
        "accuracy": float(((p_arr >= 0.5).astype(int) == y_arr).mean()),
        "ece": ece,
    }
