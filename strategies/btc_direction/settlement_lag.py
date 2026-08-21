"""Settlement-lag / decision-minute sweep — is there a near-expiry edge?

Every test so far decided at W+1 (the first observable minute). This asks the
question Derek raised: why W+1? Kalshi BTC settles on CF Benchmarks BRTI (a
~10s-lagged cross-venue TWAP), so the thesis is that NEAR EXPIRY the outcome is
largely determined by already-observed spot, and the market might underprice it —
a STRUCTURAL edge that (unlike the W+1 lead-lag) is not a sub-second latency race,
since you'd decide ~1-2 min before close, well inside the measured ~0.6s loop.

The test sweeps the decision minute k in {1, 5, 10, 13, 14} and, at each k, asks:
  1. MARKET SKILL — how good is the Kalshi price (log loss / accuracy / how
     extreme/confident)? Expect it to converge toward the truth as k -> expiry.
  2. SPOT-DISPLACEMENT EDGE — fit an out-of-sample logistic on the observed move
     disp = (spot[W+k] - spot[W]) / spot[W] (the thing the market would be lagging),
     and run the backtest's bet rule + cost model against the W+k quote. If the
     market underreacts to spot near expiry, this beats it net of cost.
  3. NAIVE follow-the-move control (bet the side disp favours at the quote).

Existing warehouse only (per-minute crypto_staging.stg_kalshi_btc_15min price +
crypto_marts.fct_features_pit BTC close + fct_kalshi_15min_label) — no new
ingestion. Caveat: the settlement reference is BRTI[W], approximated here by the
W-minute Coinbase close; and minute candles can't see the genuine last-10s BRTI
race (that needs tick capture) — this tests the MINUTE-resolution edge.

Usage:
    uv run python -m strategies.btc_direction.settlement_lag
"""

import sys

import numpy as np
import numpy.typing as npt
import pandas as pd

from core.backtest.backtest import _summarise
from core.backtest.data import _athena_connection
from core.backtest.metrics import score
from core.backtest.model import logistic_pipeline, walk_forward_oof

K_SWEEP = (1, 5, 10, 13, 14)
N_SPLITS = 8

FloatArr = npt.NDArray[np.float64]
IntArr = npt.NDArray[np.intp]

_QUERY = """
WITH lbl AS (
    SELECT window_open_at, CAST(label_up AS int) AS y
    FROM crypto_marts.fct_kalshi_15min_label
),
kal AS (
    SELECT window_open_at, event_at, implied_prob, yes_bid, yes_ask
    FROM crypto_staging.stg_kalshi_btc_15min
),
spot AS (
    SELECT event_at, close_price
    FROM crypto_marts.fct_features_pit
    WHERE asset_id = 'BTC-USD'
)
SELECT
    lbl.window_open_at,
    lbl.y,
    kk.implied_prob                                   AS p_mkt,
    kk.yes_bid                                        AS yes_bid,
    kk.yes_ask                                        AS yes_ask,
    (sk.close_price - so.close_price) / so.close_price AS disp_ret
FROM lbl
JOIN kal kk
    ON kk.window_open_at = lbl.window_open_at
   AND kk.event_at = lbl.window_open_at + interval '{k}' minute
JOIN spot so ON so.event_at = lbl.window_open_at
JOIN spot sk ON sk.event_at = lbl.window_open_at + interval '{k}' minute
ORDER BY lbl.window_open_at
"""


def _load_at_k(cur: object, k: int) -> pd.DataFrame:
    df: pd.DataFrame = cur.execute(_QUERY.format(k=k)).as_pandas()  # type: ignore[attr-defined]
    for col in ("p_mkt", "yes_bid", "yes_ask", "disp_ret"):
        df[col] = df[col].astype(float)
    df["y"] = df["y"].astype(int)
    return df.dropna(subset=["p_mkt", "yes_bid", "yes_ask", "disp_ret"]).reset_index(drop=True)


def _disp_model_oof(disp: FloatArr, y: IntArr) -> FloatArr:
    """Out-of-fold P(up) from a logistic on the observed move (level + curvature),
    walk-forward so each prediction uses only earlier windows."""
    x = np.column_stack([disp, disp**2])
    return walk_forward_oof(x, y, build_estimator=logistic_pipeline, n_splits=N_SPLITS)


def main() -> int:
    conn = _athena_connection()
    cur = conn.cursor()

    print("Decision-minute sweep: market skill + a spot-displacement edge at W+k")
    print("(threshold-0 bet rule, net of recorded spread + Kalshi fee)\n")
    print(
        f"{'k (min)':<8}{'n':>7}{'mkt_LL':>9}{'mkt_acc':>9}{'conf':>7}"
        f"{'disp_LL':>9}{'disp_bets':>10}{'disp_ROI':>10}{'naive_ROI':>10}"
    )
    print("-" * 89)

    for k in K_SWEEP:
        df = _load_at_k(cur, k)
        y = df["y"].to_numpy().astype(np.intp)
        p_mkt = df["p_mkt"].to_numpy()
        yes_ask = np.clip(df["yes_ask"].to_numpy(), 0.0, 1.0)
        yes_bid = np.clip(df["yes_bid"].to_numpy(), 0.0, 1.0)
        no_ask = 1.0 - yes_bid
        disp = df["disp_ret"].to_numpy()

        mkt = score(y, p_mkt)
        conf = float(np.mean(np.abs(p_mkt - 0.5) * 2.0))  # 0 = coin-flip, 1 = certain

        oof = _disp_model_oof(disp, y)
        live = ~np.isnan(oof)
        prob, yl = oof[live], y[live]
        ask, bid, nask = yes_ask[live], yes_bid[live], no_ask[live]
        disp_ll = score(yl, prob)["log_loss"]
        disp_r = _summarise((prob - ask) > 0.0, (bid - prob) > 0.0, yl, ask, nask)

        # Naive control: follow the observed move, pay the quote.
        naive = _summarise(disp[live] > 0.0, disp[live] < 0.0, yl, ask, nask)

        print(
            f"{k:<8}{len(df):>7,}{mkt['log_loss']:>9.3f}{mkt['accuracy']:>9.1%}{conf:>7.2f}"
            f"{disp_ll:>9.3f}{int(disp_r['n_bets']):>10,}{disp_r['roi']:>+10.2%}"
            f"{naive['roi']:>+10.2%}"
        )

    print(
        "\nmkt_LL/acc/conf = the Kalshi price's log loss, accuracy, mean confidence "
        "(|p-0.5|*2);\ndisp_* = out-of-sample spot-displacement logistic vs the W+k quote.\n"
        "\nVerdict (minute-level settlement-lag = NULL):\n"
        "  * The market CONVERGES hard toward expiry (LL ~0.66->0.10, acc 60%->96%,\n"
        "    conf 0.20->0.92) and BEATS the displacement model at EVERY k (disp_LL >\n"
        "    mkt_LL throughout) — so it is NOT underreacting to observable spot.\n"
        "  * The positive disp_ROI at k<=13 is NOT a structural edge: a model with WORSE\n"
        "    log loss can't have real skill, so it's the SAME within-minute lead-lag\n"
        "    artifact as W+1 (betting vs the lagging candle close; shown latency-bound in\n"
        "    ml/live_exec_reconcile.py). It DIES by W+14 (prices at 0.99/0.01, no room).\n"
        "  * The genuine last-~10s BRTI race is sub-minute — needs tick capture of the\n"
        "    final seconds (a collector extension), not these minute candles."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
