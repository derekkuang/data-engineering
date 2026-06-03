"""Binance Vision historical taker order-flow ingestion (public static archive).

The Binance trading API geo-blocks US IPs (HTTP 451), but the STATIC data archive
(data.binance.vision) serves historical aggTrades to US — so taker buy/sell flow,
the strongest microstructure signal for short-horizon direction, is BACKFILLABLE
(no weeks of forward collection). Each daily aggTrades zip (~12 MB) holds millions
of trades; this streams it, classifies each as aggressive-buy vs aggressive-sell
(isBuyerMaker=True => the taker SOLD), and aggregates to per-minute taker volume.
That's the ingest-tick -> aggregate-to-minute pattern (Phase-3 would push the
aggregation to Spark; pandas is fine at this volume).

Leakage guard: each minute bucket is labelled by its END (close), so a backward
as-of join at the decision minute never sees a partially-formed current minute.

Usage:
    uv run python -m ingestion.binance_flow --start 2026-03-26 --end 2026-05-31
"""

from __future__ import annotations

import argparse
import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

BASE = "https://data.binance.vision/data/spot/daily/aggTrades/BTCUSDT"
COLS = ["agg_id", "price", "qty", "first_id", "last_id", "ts", "is_buyer_maker", "is_best_match"]
OUT_PATH = Path("data/binance_btc_flow.parquet")


def minute_flow_for_day(day: date, http: httpx.Client) -> pd.DataFrame:
    """Download one day's aggTrades and aggregate to per-minute taker flow.
    Columns: minute (UTC, bucket CLOSE), buy_vol, sell_vol, n_trades."""
    url = f"{BASE}/BTCUSDT-aggTrades-{day.isoformat()}.zip"
    resp = http.get(url, timeout=120.0, follow_redirects=True)
    if resp.status_code == 404:
        return pd.DataFrame()
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        with zf.open(zf.namelist()[0]) as fh:
            df = pd.read_csv(fh, header=None, names=COLS, usecols=["qty", "ts", "is_buyer_maker"])

    # Drop a header row if the archive included one; coerce types.
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce")
    df["ts"] = pd.to_numeric(df["ts"], errors="coerce")
    df = df.dropna(subset=["qty", "ts"])

    unit = "us" if df["ts"].max() > 1e15 else "ms"  # Binance switched some feeds to microseconds
    minute = pd.to_datetime(df["ts"], unit=unit, utc=True).dt.floor("min")
    minute_close = minute + timedelta(minutes=1)
    sell = df["is_buyer_maker"].astype(str).str.lower().eq("true")  # taker sold

    rows = pd.DataFrame(
        {
            "minute": minute_close,
            "buy_vol": df["qty"].where(~sell, 0.0),
            "sell_vol": df["qty"].where(sell, 0.0),
            "n": 1,
        }
    )
    return rows.groupby("minute", as_index=False).agg(
        buy_vol=("buy_vol", "sum"), sell_vol=("sell_vol", "sum"), n_trades=("n", "sum")
    )


def backfill(start: date, end: date, out_path: Path) -> pd.DataFrame:
    http = httpx.Client()
    frames: list[pd.DataFrame] = []
    try:
        day = start
        while day < end:
            mf = minute_flow_for_day(day, http)
            if len(mf):
                frames.append(mf)
            print(f"  {day}: {len(mf):>4} minutes")
            day += timedelta(days=1)
    finally:
        http.close()

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path)
    return df


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Binance BTC taker flow (per minute)")
    parser.add_argument("--start", required=True, help="UTC date YYYY-MM-DD (inclusive)")
    parser.add_argument("--end", required=True, help="UTC date YYYY-MM-DD (exclusive)")
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC).date()
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC).date()
    df = backfill(start, end, args.out)

    print(f"\nWrote {len(df):,} minute rows to {args.out}")
    if len(df):
        tot_buy, tot_sell = df["buy_vol"].sum(), df["sell_vol"].sum()
        print(f"  range: {df['minute'].min()} .. {df['minute'].max()}")
        print(f"  total taker BTC: buy {tot_buy:,.0f}  sell {tot_sell:,.0f}")
        print(f"  total trades: {df['n_trades'].sum():,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
