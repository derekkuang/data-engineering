"""Load the leakage-free training table from Athena into a pandas DataFrame.

`fct_btc_15min_training` (Glue database `crypto_marts`) is one row per settled
KXBTC15M 15-min window: the ~25 PIT-safe BTC price/vol/momentum features plus
`kalshi_implied_prob` (the market benchmark) measured at the decision minute,
joined to `label_up` (the forward directional target). The dbt layer already
guarantees no look-ahead, so the modelling code can stay pure pandas.

This is the single read path every ML script shares — the benchmark EDA, the
walk-forward model, and the backtest all call `load_training_frame()`. Keeping
the Athena/connection details here (and nowhere else) means the modelling code
never touches boto3 or SQL.

Note: the marts live in `crypto_marts`, NOT `ATHENA_DATABASE` (which is the raw
zone, `crypto_raw`). Auth comes from the AWS credential chain, same as the
healthchecks — there are no secrets in this module.

Usage:
    from ml.data import load_training_frame
    df = load_training_frame()
"""

import os
import sys

import numpy as np
import numpy.typing as npt
import pandas as pd
from dotenv import load_dotenv
from pyathena import connect
from pyathena.connection import Connection
from pyathena.pandas.cursor import PandasCursor

MARTS_DATABASE = "crypto_marts"
TRAINING_TABLE = "fct_btc_15min_training"

# Column roles. A candidate model FEATURE is any column that is not an id, a
# timestamp, the target, or market-derived. The market-derived columns (the Kalshi
# quote) are pulled out separately because kalshi_mid_price ≈ the benchmark itself
# — letting it into "BTC features alone" would secretly hand the model the market's
# own price. They are reserved for the benchmark (the bar to beat) and the
# backtest's cost model.
ID_COLS = ["market_ticker", "asset_id"]
TIME_COLS = ["window_open_at", "window_close_at", "event_at"]
TARGET_COL = "label_up"
BENCHMARK_COL = "kalshi_implied_prob"
MARKET_COLS = ["kalshi_implied_prob", "kalshi_mid_price", "kalshi_spread"]
# Raw absolute-LEVEL columns: non-stationary, so in a walk-forward the test fold's
# level sits outside the train range (StandardScaler extrapolates) and the model can
# key on it as a time/regime proxy rather than signal. int_price_features already
# exposes the stationary counterparts (returns, dist_sma_*, rel_volume_20), so the
# raw levels are dropped from the feature set.
LEVEL_COLS = ["close_price", "volume", "dollar_volume", "signed_volume"]


def _athena_connection() -> Connection[PandasCursor]:
    """Open a pyathena connection (PandasCursor) from the standard env vars.

    PandasCursor UNLOADs the result to Parquet and reads it back with real
    dtypes — cheaper and better-typed than row-by-row fetch for a whole table.
    """
    load_dotenv()
    region = os.environ.get("AWS_REGION", "")
    workgroup = os.environ.get("ATHENA_WORKGROUP", "")
    staging_dir = os.environ.get("ATHENA_S3_STAGING_DIR", "")

    missing = [
        name
        for name, val in [
            ("AWS_REGION", region),
            ("ATHENA_WORKGROUP", workgroup),
            ("ATHENA_S3_STAGING_DIR", staging_dir),
        ]
        if not val
    ]
    if missing:
        print(f"FAIL: missing required env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    return connect(
        s3_staging_dir=staging_dir,
        region_name=region,
        work_group=workgroup,
        schema_name=MARTS_DATABASE,
        cursor_class=PandasCursor,
    )


def load_training_frame(order_chronologically: bool = True) -> pd.DataFrame:
    """Read the whole training table into a DataFrame.

    Timestamps are parsed to tz-aware UTC. When `order_chronologically` (the
    default) the rows are sorted by window_open_at — the only safe order for the
    walk-forward splits that come next, so nothing downstream can accidentally
    shuffle time.
    """
    conn = _athena_connection()
    df: pd.DataFrame = (
        conn.cursor().execute(f"SELECT * FROM {MARTS_DATABASE}.{TRAINING_TABLE}").as_pandas()
    )

    for col in TIME_COLS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True)

    if order_chronologically:
        df = df.sort_values("window_open_at").reset_index(drop=True)

    return df


def feature_columns(df: pd.DataFrame, include_market: bool = False) -> list[str]:
    """Candidate model features: every column except ids, times, and target.

    Raw absolute-level columns (LEVEL_COLS) are always excluded; market-derived
    columns (MARKET_COLS) are excluded unless include_market, so the default is a
    pure, stationary BTC feature set — the honest "can public features alone beat
    the market" question, with no market price (or non-stationary level) hidden in.
    """
    exclude = set(ID_COLS + TIME_COLS + LEVEL_COLS + [TARGET_COL])
    if not include_market:
        exclude |= set(MARKET_COLS)
    return [col for col in df.columns if col not in exclude]


def feature_matrix(
    df: pd.DataFrame,
    include_market: bool = False,
    drop: tuple[str, ...] = (),
) -> tuple[npt.NDArray[np.float64], list[str]]:
    """The numeric feature matrix the models train on, plus the column names.

    Shared by the baseline report and the backtest so they always train on the
    exact same inputs. `drop` removes named features (e.g. a stress test without
    log_return_1m); non-numeric columns are filtered out defensively.
    """
    feats = [c for c in feature_columns(df, include_market=include_market) if c not in drop]
    feats = df[feats].select_dtypes(include="number").columns.tolist()
    return df[feats].to_numpy(dtype=float), feats
