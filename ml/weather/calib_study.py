"""W0 — is the Kalshi daily-high-temperature market miscalibrated anywhere?

The taker thesis needs the market to be BEATABLE: a forecast/observation model must price the
buckets better than the market by more than spread + fee. Step zero is to find WHERE and WHEN
the market is weakest — before building any NOAA pipeline. This is the weather analog of the
BTC-15m benchmark (ml/alpha/altcoin_efficiency): fetch settled markets straight from the public
API, sample the implied probability at fixed decision times, and run the same calibration harness
(log-loss / Brier / ECE / reliability) — but on a temperature LADDER, not a single up/down binary.

Structure of the market. Each city has ~6 mutually-exclusive daily buckets ("82-83", ">=90",
"<=81") that partition the outcome and should price to a sum of ~1. Each bucket is its own YES/NO
binary settling on the NWS Climatological Report (Daily) high for the settlement station (NYC =
Central Park, LAX = Los Angeles Airport). Markets open ~10am ET the day BEFORE and last-trade
11:59pm ET the day OF, so unlike the 15-min binaries there is a ~39h price path — we sample it at
decision times relative to the measured local day.

What "calibrated" buys us. If, at a given decision time, the market's stated bucket probabilities
already match realized frequencies (ECE ~0), there is no static mispricing to harvest there and a
forecast would have to be strictly sharper than the crowd to win. Miscalibration (a decision hour
or price bin where price systematically misses realized frequency) is the first place a directional
edge could live. This is a NECESSARY, not sufficient, screen — it doesn't net out spread/fee (that's
W2); it tells us whether to keep going and where to point the pipeline.

Read-only, no auth (public market data). Scope = NYC + LAX only (the two highest-volume ladders).

Usage:
    uv run python -m ml.weather.calib_study --days 45
    uv run python -m ml.weather.calib_study --days 30 --cities NYC
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from dotenv import load_dotenv

from ingestion.kalshi import KalshiCandle, KalshiClient, normalize_market_candles
from ml.alpha.metrics import reliability_table, score

# city -> (Kalshi series, settlement station per the rules, tz of the measured local day)
CITIES: dict[str, tuple[str, str, str]] = {
    "NYC": ("KXHIGHNY", "Central Park", "America/New_York"),
    "LAX": ("KXHIGHLAX", "Los Angeles Airport", "America/Los_Angeles"),
    "CHI": ("KXHIGHCHI", "Chicago Midway", "America/Chicago"),
    "MIA": ("KXHIGHMIA", "Miami", "America/New_York"),
}
DEFAULT_CITIES = ("NYC", "LAX")

# Decision points: (label, day-offset from the measured day, ET/local clock hour). "eve" is the
# night before (pure overnight forecast, no day-of information); the day-of hours walk from early
# morning (still mostly forecast) into afternoon (the running max increasingly determines the high
# -> the "nowcast" regime that H2 targets and that picks off resting makers).
DECISION_POINTS: tuple[tuple[str, int, int], ...] = (
    ("eve", -1, 20),
    ("06h", 0, 6),
    ("09h", 0, 9),
    ("12h", 0, 12),
    ("14h", 0, 14),
    ("16h", 0, 16),
    ("18h", 0, 18),
)
CANDLE_BUDGET = 9500  # n_tickers x range_minutes must stay under ~10k per batch call


@dataclass
class Sample:
    """The market's read on ONE bucket at ONE decision time, plus its realized outcome."""

    city: str
    measured_day: dt.date
    bucket: str  # market_ticker
    dp_label: str
    prob: float  # implied_prob_close nearest the decision time
    yes_bid: float
    yes_ask: float
    outcome: int  # 1 if this bucket settled YES


def _measured_day(event_ticker: str) -> dt.date | None:
    """Parse the measured local day from an event ticker like ``KXHIGHNY-26JUL13``."""
    try:
        return dt.datetime.strptime(event_ticker.rsplit("-", 1)[-1], "%y%b%d").date()
    except ValueError:
        return None


def _chunks_under_budget(tickers: list[str], range_minutes: float) -> list[list[str]]:
    """Split tickers so ``len(chunk) x range_minutes`` stays under the candlestick budget."""
    per = max(1, int(CANDLE_BUDGET // max(range_minutes, 1.0)))
    return [tickers[i : i + per] for i in range(0, len(tickers), per)]


def fetch_city(client: KalshiClient, city: str, series: str, days: int) -> list[KalshiCandle]:
    """Hourly candles for every settled bucket of ``city`` over the last ``days``. Hourly
    (period_interval=60) keeps a ~39h ladder at ~39 bars/bucket instead of ~2,340 at 1-min —
    plenty to sample decision-time prices, and ~60x lighter on the budget and on memory."""
    now = dt.datetime.now(dt.UTC)
    start = now - dt.timedelta(days=days)
    markets = client.list_markets(
        series, status="settled",
        min_close_ts=int(start.timestamp()), max_close_ts=int(now.timestamp()),
    )
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in markets:
        by_event[m.get("event_ticker", "")].append(m)

    out: list[KalshiCandle] = []
    for event, buckets in by_event.items():
        if not _measured_day(event):
            continue
        opens = [int(_parse(m["open_time"]).timestamp()) for m in buckets]
        closes = [int(_parse(m["close_time"]).timestamp()) for m in buckets]
        lo, hi = min(opens), max(closes)
        range_min = (hi - lo) / 60.0
        tickers = [m["ticker"] for m in buckets]
        by_ticker: dict[str, list[dict[str, Any]]] = {}
        for chunk in _chunks_under_budget(tickers, range_min):
            try:
                by_ticker.update(
                    client.get_market_candlesticks_batch(
                        chunk, lo - 60, hi + 60, period_interval=60
                    )
                )
            except Exception as exc:  # noqa: BLE001  — skip a bad event, keep the study going
                print(f"  [warn] {event}: candle fetch failed ({str(exc)[:70]})", file=sys.stderr)
        for m in buckets:
            out.extend(normalize_market_candles(m, by_ticker.get(m["ticker"], []), series))
    return out


def _parse(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))


def build_samples(city: str, tz_name: str, candles: list[KalshiCandle]) -> list[Sample]:
    """For each bucket, at each decision time, take the last candle AT OR BEFORE that instant
    (point-in-time: never peek past the decision) and record the market's implied prob + outcome."""
    tz = ZoneInfo(tz_name)
    by_bucket: dict[str, list[KalshiCandle]] = defaultdict(list)
    for c in candles:
        by_bucket[c.market_ticker].append(c)

    samples: list[Sample] = []
    for bucket, cs in by_bucket.items():
        cs.sort(key=lambda c: c.event_at)
        result = cs[0].result
        if result not in ("yes", "no"):
            continue
        # measured day from the market ticker (buckets carry the event date, e.g. ..-26JUL13-B84.5)
        day = _measured_day(bucket.rsplit("-", 1)[0]) or _measured_day(bucket)
        if day is None:
            continue
        outcome = 1 if result == "yes" else 0
        for label, offset, hour in DECISION_POINTS:
            when = dt.datetime.combine(
                day + dt.timedelta(days=offset), dt.time(hour), tzinfo=tz
            ).astimezone(dt.UTC)
            prior = [c for c in cs if c.event_at <= when and c.implied_prob_close is not None]
            if not prior:
                continue
            c = prior[-1]
            if c.yes_bid_close is None or c.yes_ask_close is None:
                continue
            samples.append(Sample(
                city, day, bucket, label,
                float(c.implied_prob_close), float(c.yes_bid_close),  # type: ignore[arg-type]
                float(c.yes_ask_close), outcome,
            ))
    return samples


def report(city: str, samples: list[Sample]) -> None:
    """Per-decision-time calibration of the bucket probabilities, treating each bucket as its own
    binary. Well-calibrated (ECE ~0) everywhere = no static edge; a hot decision-hour/price-bin =
    where to point the forecast."""
    print(f"\n{'=' * 68}\n{city}: {len({s.measured_day for s in samples})} settled days, "
          f"{len(samples)} bucket-observations\n{'=' * 68}")
    print(f"{'when':<6}{'n':>6}{'logloss':>9}{'brier':>8}{'ECE':>7}{'medSpr':>8}   worst price-bin")
    for label, _, _ in DECISION_POINTS:
        rows = [s for s in samples if s.dp_label == label]
        if len(rows) < 30:
            print(f"{label:<6}{len(rows):>6}   (too few for a read)")
            continue
        y = np.array([s.outcome for s in rows])
        p = np.array([s.prob for s in rows])
        spr = np.array([(s.yes_ask - s.yes_bid) for s in rows])
        s = score(y, p)
        tbl = reliability_table(y, p)
        tail = tbl[tbl["n"] >= 10]
        worst = ""
        if len(tail):
            w = tail.iloc[(tail["pred_mean"] - tail["obs_freq"]).abs().argmax()]
            worst = (f"{w['bin']} price {w['pred_mean']:.2f} vs real {w['obs_freq']:.2f} "
                     f"(n={int(w['n'])})")
        print(f"{label:<6}{int(s['n']):>6}{s['log_loss']:>9.3f}{s['brier']:>8.3f}"
              f"{s['ece']:>7.3f}{np.median(spr) * 100:>7.1f}c   {worst}")


def main() -> int:
    ap = argparse.ArgumentParser(description="W0 weather calibration study (NYC+LAX temp ladders)")
    ap.add_argument("--days", type=int, default=45, help="settled history to fetch per city")
    ap.add_argument("--cities", default=",".join(DEFAULT_CITIES),
                    help=f"comma-separated, from {sorted(CITIES)}")
    args = ap.parse_args()

    load_dotenv()
    client = KalshiClient()
    cities = [c.strip().upper() for c in args.cities.split(",") if c.strip()]
    print(f"Fetching ~{args.days}d of settled temp ladders for {cities} "
          f"(hourly candles, {len(DECISION_POINTS)} decision times)...")
    try:
        for city in cities:
            if city not in CITIES:
                print(f"  [skip] unknown city {city}", file=sys.stderr)
                continue
            series, station, tz_name = CITIES[city]
            candles = fetch_city(client, city, series, args.days)
            samples = build_samples(city, tz_name, candles)
            if not samples:
                print(f"\n{city}: no usable samples (settles on {station}).")
                continue
            report(city, samples)
    finally:
        client.close()

    print("\nRead: ECE ~0 across decision times = the crowd already prices the buckets well, so a\n"
          "forecast must be strictly SHARPER than the market to win — a high bar (the BTC story).\n"
          "A decision hour or price bin where price misses realized frequency is the first place\n"
          "H1 (model, morning) or H2 (nowcast, afternoon) could pay. This screen does NOT net out\n"
          "spread + fee — that is W2. It only says whether, and where, to build the NOAA pipeline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
