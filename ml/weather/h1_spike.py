"""W1-spike — does a weather MODEL out-price the morning market? (cheap H1 go/no-go)

W0 showed the temp ladders are well-calibrated except for a residual opening in the MORNING
window. H1 is the hypothesis that a numerical forecast, known that morning, prices the buckets
better than the crowd does. Before building the full NOAA medallion pipeline (W1), this spike
answers the go/no-go on history for ~$0:

  * MARKET read  — the morning implied bucket probabilities (reuses ml.weather.calib_study).
  * MODEL read   — Open-Meteo's archived ~morning-of daily-high forecast per station, turned into
                   ladder probabilities via a Gaussian error model P(bucket) = Φ(hi) − Φ(lo).
  * TRUTH        — which bucket actually settled YES (what pays).

Both are scored as multi-class log-loss over each day's ladder (normalized to sum 1), averaged
over days. If the model can't beat the market here it certainly won't after spread + fee → weather
parks. If it does, we re-verify with a lead-time-pinned forecast (the honest W1) before believing.

Two honest caveats, both making this a CONSERVATIVE kill test (they favor the model, so a loss is
decisive): (1) Open-Meteo's default archived forecast is ~0-day lead and may fold in intra-day
updates → mild look-ahead vs a true morning forecast; (2) σ is SWEPT (and the best reported), i.e.
fit in-sample. A model win under both thumbs-on-the-scale would still need the honest re-test.

Usage:
    uv run python -m ml.weather.h1_spike --days 45
    uv run python -m ml.weather.h1_spike --days 45 --when 09h
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import defaultdict
from typing import Any

import numpy as np
import requests
from dotenv import load_dotenv
from scipy.stats import norm

from ingestion.kalshi import KalshiClient
from ml.weather.calib_study import CITIES, DEFAULT_CITIES, _measured_day, build_samples, fetch_city

# Settlement-station coordinates (lat, lon) — the point the NWS high is read at, so the forecast
# is drawn at the same spot the market settles on.
STATIONS: dict[str, tuple[float, float]] = {
    "NYC": (40.7789, -73.9692),   # Central Park
    "LAX": (33.9416, -118.4085),  # Los Angeles Airport
    "CHI": (41.7860, -87.7524),   # Chicago Midway
    "MIA": (25.7906, -80.3164),   # Miami Intl
}
SIGMA_SWEEP = (1.5, 2.0, 2.5, 3.0, 3.5)  # °F forecast-error std to try (day-ahead tmax RMSE ~2-3°F)
HIST_FORECAST = "https://historical-forecast-api.open-meteo.com/v1/forecast"
EPS = 1e-6


def fetch_forecasts(city: str, days: int) -> dict[dt.date, float]:
    """Archived daily-high forecast (°F) per measured day for the station."""
    lat, lon = STATIONS[city]
    end = dt.date.today()
    start = end - dt.timedelta(days=days + 2)
    tz = CITIES[city][2]
    params: dict[str, str | float] = {
        "latitude": lat, "longitude": lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "temperature_2m_max", "temperature_unit": "fahrenheit", "timezone": tz,
    }
    resp = requests.get(HIST_FORECAST, params=params, timeout=60)
    resp.raise_for_status()
    daily = resp.json().get("daily", {})
    out: dict[dt.date, float] = {}
    for iso, tmax in zip(daily.get("time", []), daily.get("temperature_2m_max", []), strict=False):
        if tmax is not None:
            out[dt.date.fromisoformat(iso)] = float(tmax)
    return out


def bucket_bounds(strike_type: str, floor: float | None, cap: float | None) -> tuple[float, float]:
    """Continuous [lo, hi) high-temperature interval a bucket covers. NWS reports integer °F, so a
    'between' floor=84 cap=85 catches a true high in [83.5, 85.5); tails are half-open to ±inf."""
    if strike_type == "between" and floor is not None and cap is not None:
        return floor - 0.5, cap + 0.5
    if strike_type == "greater" and floor is not None:  # "90 or above" => high >= 90 => X >= 89.5
        return floor + 0.5, float("inf")
    if strike_type == "less" and cap is not None:        # "81 or below" => high <= 81 => X < 81.5
        return float("-inf"), cap - 0.5
    return float("nan"), float("nan")


def model_prob(bounds: tuple[float, float], mu: float, sigma: float) -> float:
    lo, hi = bounds
    return float(norm.cdf(hi, mu, sigma) - norm.cdf(lo, mu, sigma))


def _ladder_logloss(probs: dict[str, float], winner: str) -> float:
    """Multi-class log-loss for one day: normalize the ladder to sum 1, score the winning bucket."""
    tot = sum(probs.values())
    if tot <= 0:
        return float("nan")
    p = max(probs[winner] / tot, EPS)
    return -float(np.log(p))


def run(city: str, days: int, when: str) -> dict[str, float] | None:
    load_dotenv()
    client = KalshiClient()
    series = CITIES[city][0]
    try:
        markets = client.list_markets(
            series, status="settled",
            min_close_ts=int((dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).timestamp()),
            max_close_ts=int(dt.datetime.now(dt.UTC).timestamp()),
        )
        candles = fetch_city(client, city, series, days)
    finally:
        client.close()

    # bucket metadata keyed by ticker: bounds + winner flag + measured day
    meta: dict[str, dict[str, Any]] = {}
    winners: dict[dt.date, str] = {}
    for m in markets:
        day = _measured_day(m.get("event_ticker", ""))
        if day is None:
            continue
        b = bucket_bounds(m.get("strike_type", ""), m.get("floor_strike"), m.get("cap_strike"))
        if b[0] != b[0]:  # NaN -> unparseable strike
            continue
        meta[m["ticker"]] = {"bounds": b, "day": day}
        if m.get("result") == "yes":
            winners[day] = m["ticker"]

    # morning market probs from W0's sampler, grouped by day
    samples = [s for s in build_samples(city, CITIES[city][2], candles) if s.dp_label == when]
    mkt_by_day: dict[dt.date, dict[str, float]] = defaultdict(dict)
    for s in samples:
        mkt_by_day[s.measured_day][s.bucket] = s.prob

    forecasts = fetch_forecasts(city, days)

    mkt_ll: list[float] = []
    model_ll: dict[float, list[float]] = {sig: [] for sig in SIGMA_SWEEP}
    mkt_hit = model_hit = n = 0
    resid: list[float] = []
    for day, mkt in mkt_by_day.items():
        if day not in winners or day not in forecasts:
            continue
        winner = winners[day]
        if winner not in mkt:  # need the winning bucket's morning price for a fair market score
            continue
        mu = forecasts[day]
        # realized-high proxy = winning bucket mid, for a σ sanity read
        lo, hi = meta[winner]["bounds"]
        if np.isfinite(lo) and np.isfinite(hi):
            mid = (lo + hi) / 2
        else:
            mid = lo if np.isfinite(lo) else hi
        if np.isfinite(mid):
            resid.append(mid - mu)
        n += 1
        mkt_ll.append(_ladder_logloss(mkt, winner))
        if max(mkt, key=lambda b: mkt[b]) == winner:
            mkt_hit += 1
        best_model_argmax_hit = False
        for sig in SIGMA_SWEEP:
            mp = {tk: model_prob(meta[tk]["bounds"], mu, sig) for tk in mkt if tk in meta}
            model_ll[sig].append(_ladder_logloss(mp, winner))
            if sig == 2.5 and max(mp, key=lambda b: mp[b]) == winner:
                best_model_argmax_hit = True
        model_hit += int(best_model_argmax_hit)

    if n < 15:
        print(f"\n{city}: only {n} usable days — too few for a read.")
        return None

    best_sig = min(SIGMA_SWEEP, key=lambda s: float(np.mean(model_ll[s])))
    r = {
        "n": n, "mkt_ll": float(np.mean(mkt_ll)),
        "model_ll": float(np.mean(model_ll[best_sig])), "best_sig": best_sig,
        "mkt_hit": mkt_hit / n, "model_hit": model_hit / n,
        "resid_bias": float(np.mean(resid)) if resid else float("nan"),
        "resid_std": float(np.std(resid)) if resid else float("nan"),
    }
    winner_lbl = "MODEL better" if r["model_ll"] < r["mkt_ll"] else "market better"
    print(f"\n{'=' * 64}\n{city} — {n} days, morning decision point '{when}'\n{'=' * 64}")
    print(f"  forecast vs realized-bucket-mid: bias {r['resid_bias']:+.1f}°F, "
          f"std {r['resid_std']:.1f}°F")
    print(f"  ladder log-loss   MARKET {r['mkt_ll']:.3f}   MODEL {r['model_ll']:.3f} "
          f"(best σ={best_sig})  -> {winner_lbl}")
    print("  per-σ model log-loss: " + "  ".join(
        f"σ{s}={np.mean(model_ll[s]):.3f}" for s in SIGMA_SWEEP))
    print(f"  argmax-bucket hit rate   MARKET {r['mkt_hit']:.0%}   "
          f"MODEL(σ2.5) {r['model_hit']:.0%}")
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description="H1 spike: weather model vs morning market (NYC+LAX)")
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--cities", default=",".join(DEFAULT_CITIES))
    ap.add_argument("--when", default="06h", help="morning decision point (06h/09h/eve)")
    args = ap.parse_args()

    results = {}
    for city in (c.strip().upper() for c in args.cities.split(",")):
        if city not in STATIONS:
            print(f"[skip] {city}: no station coords", file=sys.stderr)
            continue
        r = run(city, args.days, args.when)
        if r:
            results[city] = r

    print(f"\n{'=' * 64}\nVERDICT\n{'=' * 64}")
    if not results:
        print("No usable results.")
        return 0
    any_edge = False
    for city, r in results.items():
        delta = r["mkt_ll"] - r["model_ll"]
        verdict = "MODEL beats market" if delta > 0 else "market wins"
        any_edge = any_edge or delta > 0.02
        print(f"  {city}: Δlog-loss {delta:+.3f} ({verdict}); "
              f"model needs to beat market by more than the spread+fee gap (~W2).")
    print("\n" + (
        "At least one city shows a model edge on log-loss -> WORTH the honest W1 re-test with\n"
        "a lead-pinned forecast + a spread/fee-net backtest before believing it."
        if any_edge else
        "Model does NOT beat the morning market on either city -> H1 is weak/dead. The crowd\n"
        "already embeds the forecast; a taker edge would need to be sharper than Open-Meteo, a\n"
        "very high bar. Recommend PARK weather-directional (or pivot to H2 nowcast only) rather\n"
        "than build the full pipeline. Consistent with the BTC finding: liquid markets efficient."
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
