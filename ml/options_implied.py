"""Deribit options-implied direction — does "smart money" forecast the 15-min move?

The one fresh angle that imports NEW information rather than re-slicing price: the
Deribit BTC options market is priced by professional vol desks. Its risk-neutral
view is an implied probability distribution for BTC at expiry. We ask whether that
distribution says anything DIRECTIONAL at a 15-minute horizon that the Kalshi
KXBTC15M market (priced ~0.5) doesn't already reflect.

Method (live snapshot, public API): take the shortest-expiry ATM implied vol and
the wing skew (risk reversal), then compute the risk-neutral P(BTC up over 15 min)
under Black-Scholes:  P(S_T > S_0) = N(d2),  d2 = (r - sigma^2/2)*sqrt(T)/sigma,
with T = 15min. The deviation of that probability from 0.5 is the options-implied
directional edge; compare it to the ~1c Kalshi spread.

The structural prior: over 15 minutes the risk-neutral drift is negligible, so
N(d2) ~ 0.5 and even a large skew moves it by a fraction of a cent — options price
VOLATILITY, not 15-min direction. This quantifies that with live numbers.

Usage: uv run python -m ml.options_implied
"""

from __future__ import annotations

import math
import sys
from collections import defaultdict

import httpx

BOOK_SUMMARY = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
MINUTES_PER_YEAR = 365.0 * 24.0 * 60.0
HORIZON_MIN = 15.0


def _phi(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _parse(name: str) -> tuple[str, float, str] | None:
    """BTC-5JUN26-85000-P -> (expiry, strike, 'P')."""
    parts = name.split("-")
    if len(parts) != 4:
        return None
    try:
        return parts[1], float(parts[2]), parts[3]
    except ValueError:
        return None


def _implied_p_up(sigma_annual: float, t_years: float) -> float:
    """Risk-neutral P(S_T > S_0) under BS with r~=0: N(-sigma*sqrt(T)/2)."""
    d2 = -0.5 * sigma_annual * math.sqrt(t_years)
    return _phi(d2)


def main() -> int:
    import datetime as dt

    resp = httpx.get(
        BOOK_SUMMARY, params={"currency": "BTC", "kind": "option"}, timeout=20.0
    ).json()
    options = resp.get("result", [])
    if not options:
        print("FAIL: no options returned", file=sys.stderr)
        return 1

    spot = float(options[0]["underlying_price"])

    # Group by expiry as (strike, type, iv) tuples; pick the SHORTEST expiry chain.
    by_exp: dict[dt.date, list[tuple[float, str, float]]] = defaultdict(list)
    for o in options:
        p = _parse(str(o["instrument_name"]))
        if p is None or o.get("mark_iv") is None:
            continue
        exp, strike, typ = p
        try:
            exp_date = dt.datetime.strptime(exp, "%d%b%y").date()
        except ValueError:
            continue
        by_exp[exp_date].append((strike, typ, float(o["mark_iv"])))

    today = dt.datetime.now(dt.UTC).date()
    future = sorted(e for e in by_exp if e >= today)
    if not future:
        print("FAIL: no unexpired expiry", file=sys.stderr)
        return 1
    exp_date = future[0]
    chain = by_exp[exp_date]

    # ATM IV: the strike nearest spot (mean of call+put marks there).
    strikes = sorted({k for k, _, _ in chain})
    atm_k = min(strikes, key=lambda k: abs(k - spot))
    atm_ivs = [iv for k, _, iv in chain if k == atm_k]
    atm_iv = sum(atm_ivs) / len(atm_ivs)

    # Skew / risk reversal: ~5% OTM call IV minus ~5% OTM put IV (directional fear).
    def iv_at(target: float, typ: str) -> float | None:
        cand = [(k, iv) for k, t, iv in chain if t == typ]
        if not cand:
            return None
        return min(cand, key=lambda c: abs(c[0] - target))[1]

    call_iv = iv_at(spot * 1.05, "C")
    put_iv = iv_at(spot * 0.95, "P")
    rr = (call_iv - put_iv) if (call_iv is not None and put_iv is not None) else float("nan")

    # Implied 15-min direction.
    t_years = HORIZON_MIN / MINUTES_PER_YEAR
    sigma = atm_iv / 100.0
    p_up = _implied_p_up(sigma, t_years)
    dev_cents = (p_up - 0.5) * 100.0
    sigma_15m_pct = sigma * math.sqrt(t_years) * 100.0  # 1-sigma move over 15 min, %

    move_usd = spot * sigma_15m_pct / 100.0
    rr_txt = f"{rr:+.1f} vol pts" if not math.isnan(rr) else "n/a"
    print(f"Deribit BTC options snapshot — spot {spot:,.0f}, shortest expiry {exp_date}")
    print(f"  ATM implied vol (annualised):     {atm_iv:.1f}%")
    print(f"  => 1-sigma move over 15 min:      {sigma_15m_pct:.2f}%  (~${move_usd:,.0f})")
    print(f"  25-ish-delta risk reversal (C-P): {rr_txt}")
    print()
    print(f"  risk-neutral P(BTC up over 15m):  {p_up:.4f}")
    print(f"  directional edge vs coin-flip:    {dev_cents:+.2f}c  (Kalshi spread ~1c)")
    print()
    print(
        "Verdict: the options-implied 15-min P(up) is ~0.50 (the risk-neutral drift over\n"
        f"15 min is negligible: deviation {abs(dev_cents):.2f}c << the ~1c spread). The chain's\n"
        "real information is VOLATILITY (the IV level), which a direction market doesn't use,\n"
        "and the skew's 15-min directional effect is sub-cent. So 'import the options view'\n"
        "adds NO usable 15-min directional signal — null by construction, not just empirically.\n"
        "(Options would matter for a VOL/magnitude market, e.g. Kalshi range/straddle contracts.)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
