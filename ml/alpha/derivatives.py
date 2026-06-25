"""Derivatives features merged onto the training frame (Deribit funding).

Funding is a slow (8-hourly) positioning signal, so it is joined point-in-time as a
BACKWARD as-of merge: each window's decision minute (event_at) gets the most recent
funding observation at or before it — never a future one. That keeps the same PIT
contract as the rest of the feature store. Adds:
  funding_rate    : the 8h funding rate known as of the decision minute (positioning)
  funding_chg_8h  : funding now minus funding 8h earlier (positioning momentum)

Reads the local Parquet cache written by ingestion/deribit.py.
"""

from pathlib import Path

import pandas as pd

FUNDING_PATH = Path("data/deribit_funding.parquet")


def add_funding_features(df: pd.DataFrame, funding_path: Path = FUNDING_PATH) -> pd.DataFrame:
    """Attach funding_rate + funding_chg_8h to each row by backward as-of join on
    event_at (the decision minute). Original row order is preserved so the
    downstream walk-forward split stays chronological."""
    # drop_duplicates first: chunk-boundary repeats would break the hourly grid that
    # the shift(8) ("funding 8 hours ago") momentum feature assumes.
    funding = pd.read_parquet(funding_path).drop_duplicates("funding_time")
    funding = funding.sort_values("funding_time").reset_index(drop=True)
    funding["funding_chg_8h"] = funding["interest_8h"] - funding["interest_8h"].shift(8)
    funding = funding.rename(columns={"interest_8h": "funding_rate"})[
        ["funding_time", "funding_rate", "funding_chg_8h"]
    ]

    out = df.copy()
    out["_orig"] = range(len(out))
    out = out.sort_values("event_at")
    # merge_asof needs identical datetime resolution on both keys (event_at is us,
    # funding_time is ms from the parquet) — normalise both to ns.
    out["event_at"] = out["event_at"].dt.as_unit("ns")
    funding["funding_time"] = funding["funding_time"].dt.as_unit("ns")
    merged = pd.merge_asof(
        out, funding, left_on="event_at", right_on="funding_time", direction="backward"
    )
    merged = merged.sort_values("_orig").drop(columns=["_orig", "funding_time"])
    return merged.reset_index(drop=True)
