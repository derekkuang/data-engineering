"""W1-probe — model-vs-market PERSISTENCE on Kalshi daily-high temperature ladders
(READ-ONLY, $0). Tests whether the "seconds-fast NBM arb" the pros run is capturable at
OUR (non-co-located) latency, before believing any of it.

The systematic weather winners price the buckets off a calibrated forecast (NBM) and trade
when the market deviates from it by more than spread+fee — a speed race the loss
post-mortem (docs/research) says closes in "seconds" against co-located bots. This logger
measures that claim directly: build an empirical daily-high distribution from a free
ensemble, map it to the live Kalshi ladder, and record every moment a bucket deviates from
the model by more than the Kalshi fee hurdle — how often, how large, how long it persists.

- NO fee-clearing, persistent deviation ever seen  -> the market tracks the model inside our
  reaction time; the arb is a co-located game, closed for us (like the BRTI tick race).
- Persistent fee-clearing deviations DO appear      -> UNEXPECTED; worth the NBM-proper
  (grib percentile) upgrade + an execution conversation.

MODEL: free Open-Meteo GEFS ensemble (31 members) -> empirical distribution of the daily
high (max of hourly temps per member, rounded to the integer °F Kalshi settles on). Using
the ensemble EMPIRICALLY (not a normal-CDF) fixes the fat-tail bug the post-mortem flagged.
This is the accessible ANALOG of NBM; NBM-proper (calibrated station percentiles, grib2)
is the upgrade IF this probe shows signal. CAVEAT: an hourly-sampled max slightly
underprices the true 1-min ASOS max (~0.5–1°F low) — a known bias NBM corrects; here it
shows as a systematic skew, itself informative.

Usage::

    uv run python -m strategies.weather_taker.nbm_market_probe --seconds 300
    uv run python -m strategies.weather_taker.nbm_market_probe --cities NYC,LAX --seconds 600
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import re
import time
from dataclasses import dataclass, field

import httpx

KALSHI = "https://external-api.kalshi.com/trade-api/v2"
ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"

# city -> (Kalshi series, settlement-station lat, lon, tz of the measured local day).
# Stations per Kalshi's rules (NYC=Central Park, LAX/CHI/MIA/AUS = station of record).
CITIES: dict[str, tuple[str, float, float, str]] = {
    "NYC": ("KXHIGHNY", 40.7789, -73.9692, "America/New_York"),
    "LAX": ("KXHIGHLAX", 33.9382, -118.3866, "America/Los_Angeles"),
    "CHI": ("KXHIGHCHI", 41.7860, -87.7524, "America/Chicago"),
    "MIA": ("KXHIGHMIA", 25.7906, -80.3164, "America/New_York"),
    "AUS": ("KXHIGHAUS", 30.3210, -97.7594, "America/Chicago"),
}


def fee_cents(price_cents: float) -> float:
    """Kalshi trading fee per contract: ceil(0.07 · P · (1−P)) in cents (~1.75c at mid)."""
    p = max(0.0, min(1.0, price_cents / 100.0))
    return math.ceil(7.0 * p * (1.0 - p))


def parse_bucket(subtitle: str) -> tuple[float, float] | None:
    """Turn a Kalshi high-temp bucket label into an inclusive integer-°F range:
    '82° or below' -> (-inf, 82); '91° or above' -> (91, +inf); '83° to 84°' -> (83, 84)."""
    nums = [int(x) for x in re.findall(r"-?\d+", subtitle)]
    low = subtitle.lower()
    if "below" in low and nums:
        return (-math.inf, float(nums[0]))
    if "above" in low and nums:
        return (float(nums[0]), math.inf)
    if len(nums) >= 2:
        return (float(nums[0]), float(nums[1]))
    return None


@dataclass(frozen=True)
class Bucket:
    ticker: str
    subtitle: str
    lo: float
    hi: float
    yes_bid: float  # cents
    yes_ask: float


def measured_day(event_ticker: str) -> dt.date | None:
    """Parse the local day from an event ticker like KXHIGHNY-26SEP02."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", event_ticker)
    if not m:
        return None
    yy, mon, dd = m.groups()
    months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    try:
        return dt.date(2000 + int(yy), months.index(mon) + 1, int(dd))
    except ValueError:
        return None


def fetch_ladder(c: httpx.Client, series: str) -> tuple[str, dt.date, list[Bucket]] | None:
    """The nearest open, QUOTED high-temp event for a series (or None if none is live —
    overnight books are empty; weather flow is intraday US)."""
    r = c.get(f"{KALSHI}/events",
              params={"series_ticker": series, "status": "open", "with_nested_markets": "true",
                      "limit": 6})
    r.raise_for_status()
    for e in r.json().get("events", []):
        buckets = []
        for m in e.get("markets") or []:
            if m.get("yes_bid") is None or m.get("yes_ask") is None:
                continue
            rng = parse_bucket(str(m.get("yes_sub_title", "")))
            if rng is None:
                continue
            buckets.append(Bucket(m["ticker"], str(m.get("yes_sub_title", "")),
                                  rng[0], rng[1], float(m["yes_bid"]), float(m["yes_ask"])))
        if buckets:
            day = measured_day(e["event_ticker"]) or dt.date.today()
            return (e["event_ticker"], day, buckets)
    return None


def ensemble_daily_high(c: httpx.Client, lat: float, lon: float, tz: str,
                        day: dt.date) -> list[int] | None:
    """Empirical distribution of the daily high (°F, integer-rounded) across GEFS members
    for `day` — max of each member's hourly temps on that local calendar day."""
    r = c.get(ENSEMBLE, params={
        "latitude": lat, "longitude": lon, "hourly": "temperature_2m",
        "models": "gfs_seamless", "temperature_unit": "fahrenheit",
        "timezone": tz, "forecast_days": 4,
    })
    r.raise_for_status()
    h = r.json().get("hourly", {})
    times = h.get("time", [])
    members = [k for k in h if k.startswith("temperature_2m")]
    if not times or not members:
        return None
    target = day.isoformat()
    idx = [i for i, t in enumerate(times) if t[:10] == target]
    if not idx:
        return None
    highs = []
    for mem in members:
        vals = [h[mem][i] for i in idx if h[mem][i] is not None]
        if vals:
            highs.append(round(max(vals)))
    return highs or None


def model_prob(highs: list[int], b: Bucket) -> float:
    """P(daily high ∈ bucket) from the empirical member distribution."""
    n = len(highs)
    if not n:
        return 0.0
    hit = sum(1 for x in highs if b.lo <= x <= b.hi)
    return hit / n


@dataclass
class BucketStats:
    city: str
    ticker: str
    subtitle: str
    polls: int = 0
    signals: int = 0        # polls with a fee-clearing model-vs-market edge
    max_ev: float = -99.0   # best net-of-fee EV seen (cents/contract)
    max_ev_side: str = ""
    episodes: list[int] = field(default_factory=list)
    _run: int = 0

    def observe(self, ev: float, side: str) -> None:
        self.polls += 1
        if ev > 0:
            self.signals += 1
            self._run += 1
            if ev > self.max_ev:
                self.max_ev, self.max_ev_side = ev, side
        else:
            if self._run:
                self.episodes.append(self._run)
            self._run = 0

    def close(self) -> None:
        if self._run:
            self.episodes.append(self._run)


def edge(b: Bucket, mp: float) -> tuple[float, str]:
    """Best net-of-fee EV across the two executable sides (cents/contract):
    BUY YES at ask (payoff mp·100) or BUY NO at 100−yes_bid (payoff (1−mp)·100)."""
    f = fee_cents((b.yes_bid + b.yes_ask) / 2.0)
    buy_yes = mp * 100.0 - b.yes_ask - f
    buy_no = (1.0 - mp) * 100.0 - (100.0 - b.yes_bid) - f
    return (buy_yes, "YES") if buy_yes >= buy_no else (buy_no, "NO")


def run(cities: list[str], seconds: int, poll_gap: float) -> list[BucketStats]:
    with httpx.Client(timeout=20, headers={"User-Agent": "crypto-de/weather"}) as c:
        live: dict[str, tuple[dt.date, list[Bucket], list[int]]] = {}
        for city in cities:
            series, lat, lon, tz = CITIES[city]
            lad = fetch_ladder(c, series)
            if lad is None:
                print(f"  {city}: no live quoted ladder (intraday US only) — skipping")
                continue
            _, day, buckets = lad
            highs = ensemble_daily_high(c, lat, lon, tz, day)
            if not highs:
                print(f"  {city}: no ensemble for {day} — skipping")
                continue
            live[city] = (day, buckets, highs)
            print(f"  {city} {day}: {len(buckets)} quoted buckets, {len(highs)} ensemble members "
                  f"(model high med {sorted(highs)[len(highs)//2]}°F)")
        if not live:
            print("\nNo live weather market right now — run during a US intraday window "
                  "(the books are empty overnight). Plumbing OK.")
            return []
        stats = {
            (city, b.ticker): BucketStats(city, b.ticker, b.subtitle)
            for city, (_, buckets, _) in live.items() for b in buckets
        }
        print(f"\npolling {len(stats)} buckets across {len(live)} cities for {seconds}s...\n")
        t_end = time.time() + seconds
        rounds = 0
        while time.time() < t_end:
            for city, (_, _, highs) in live.items():
                series = CITIES[city][0]
                lad = fetch_ladder(c, series)
                if lad is None:
                    continue
                for b in lad[2]:
                    key = (city, b.ticker)
                    if key not in stats:
                        continue
                    ev, side = edge(b, model_prob(highs, b))
                    stats[key].observe(ev, side)
            rounds += 1
            if rounds % 5 == 0:
                sig = sum(s.signals for s in stats.values())
                print(f"  round {rounds}: {sig} fee-clearing signal-polls so far")
            time.sleep(poll_gap)
        for s in stats.values():
            s.close()
        return list(stats.values())


def report(stats: list[BucketStats], seconds: int) -> None:
    if not stats:
        return
    print("\n" + "=" * 96)
    print("WEATHER MODEL-vs-MARKET PERSISTENCE — fee-clearing deviation capturable at our latency?")
    print("=" * 96)
    print(f"{'polls':>6}{'signal':>7}{'maxEV':>8}{'side':>5}{'longestRun':>11}  city  bucket")
    print("-" * 96)
    tot_polls = tot_sig = 0
    for s in sorted(stats, key=lambda x: x.max_ev, reverse=True):
        longest = max(s.episodes) if s.episodes else 0
        tot_polls += s.polls
        tot_sig += s.signals
        print(f"{s.polls:>6}{s.signals:>7}{s.max_ev:>+8.2f}{s.max_ev_side:>5}{longest:>11}  "
              f"{s.city:4s}  {s.subtitle[:26]}")
    print("-" * 96)
    print(f"{tot_polls} bucket-polls over {seconds}s | {tot_sig} showed a fee-clearing edge")
    if tot_sig == 0:
        print("VERDICT: NO fee-clearing model-vs-market deviation observed — the market tracks the "
              "model inside our latency. Arb closed for us (co-located game).")
    else:
        print(f"VERDICT: {tot_sig} fee-clearing signal-poll(s) — inspect persistence + the "
              "hourly-max bias before believing; if persistent, NBM-proper is the next lift.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cities", type=str, default="NYC,LAX,CHI,MIA,AUS",
                    help="comma-separated subset of " + ",".join(CITIES))
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--poll-gap", type=float, default=2.0)
    args = ap.parse_args()
    cities = [x.strip().upper() for x in args.cities.split(",") if x.strip().upper() in CITIES]
    stats = run(cities, args.seconds, args.poll_gap)
    report(stats, args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
