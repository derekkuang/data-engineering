"""Order-flow features (Binance taker imbalance) merged onto the training frame.

Reads per-minute taker buy/sell volume (ingestion/binance_flow.py), computes rolling
BACKWARD imbalance over 5/15/60 min ending at each minute close, and PIT-joins them
at the decision minute by backward as-of. OFI = (taker_buy - taker_sell)/(buy+sell)
in [-1, 1] — net aggressive-buy pressure, the classic short-horizon flow signal that
plain OHLCV throws away.

Leakage note: minutes are END-labelled in ingestion and the as-of join is backward,
so a decision at W+1 sees only flow through the W..W+1 minute (same horizon as the
price features) and never the rest of the labelled window.
"""

from pathlib import Path

import pandas as pd

FLOW_PATH = Path("data/binance_btc_flow.parquet")
WINDOWS = (5, 15, 60)


def add_orderflow_features(df: pd.DataFrame, flow_path: Path = FLOW_PATH) -> pd.DataFrame:
    """Attach ofi_5m/15m/60m + flow_vol_15m to each row by backward as-of join on
    event_at. Original row order is preserved so the walk-forward split stays
    chronological."""
    # drop_duplicates: a repeated minute (e.g. overlapping daily files) would
    # double-count the rolling sums and make the as-of match ambiguous.
    flow = pd.read_parquet(flow_path).drop_duplicates("minute").sort_values("minute")
    flow = flow.reset_index(drop=True)
    cols = []
    for w in WINDOWS:
        buy = flow["buy_vol"].rolling(w, min_periods=w).sum()
        sell = flow["sell_vol"].rolling(w, min_periods=w).sum()
        flow[f"ofi_{w}m"] = (buy - sell) / (buy + sell)
        cols.append(f"ofi_{w}m")
    flow["flow_vol_15m"] = (flow["buy_vol"] + flow["sell_vol"]).rolling(15, min_periods=15).sum()
    cols.append("flow_vol_15m")
    feat = flow[["minute", *cols]].copy()
    feat["minute"] = feat["minute"].dt.as_unit("ns")

    out = df.copy()
    out["_orig"] = range(len(out))
    out = out.sort_values("event_at")
    out["event_at"] = out["event_at"].dt.as_unit("ns")
    merged = pd.merge_asof(
        out, feat, left_on="event_at", right_on="minute", direction="backward"
    )
    merged = merged.sort_values("_orig").drop(columns=["_orig", "minute"])
    return merged.reset_index(drop=True)
