"""Threshold-market static-arbitrage scan — a no-race, no-forecast edge test.

The 15-min direction edge is a latency race we lose. This tests a structurally
different angle that needs NO forecast and NO speed: static mispricings in the
KXBTCD / KXETHD threshold ladders ("BTC/ETH >= strike at expiry?"). For one expiry
there is a ladder of strikes, and the implied probability P(>= K) MUST be
non-increasing in K. Two risk-free relationships, checkable from a single snapshot:

  1. MONOTONICITY (mid): P(>= K2) <= P(>= K1) for K2 > K1. A mid that rises with
     strike is a pricing inconsistency.
  2. EXECUTABLE arb (the bankable one): for K1 < K2, buy YES@K1 + NO@K2. That combo
     pays $1 if BTC<K1, $2 if K1<=BTC<K2, $1 if BTC>=K2 -> ALWAYS >= $1. So if its
     cost yes_ask(K1) + no_ask(K2) + fees < $1, it is free money. (Equivalently:
     yes_ask(K1) < yes_bid(K2) before fees.)

Unlike the lead-lag, capturing this is not a microsecond race against market
makers — the mispricing only has to persist long enough to hit two legs. The
honest prior (from research + the BTC efficiency we keep finding) is that any gap
sits inside fees; ETH/SOL ladders, being thinner, are likelier to show something.

Read-only public market data. Usage:
    uv run python -m strategies.btc_direction.threshold_arb
"""

from __future__ import annotations

import math
import sys
from collections import defaultdict
from typing import Any

from dotenv import load_dotenv

from ingestion.kalshi import KalshiClient

SERIES = ("KXBTCD", "KXETHD")
FEE_RATE = 0.07  # Kalshi per-contract fee = ceil_to_cent(0.07 * p * (1-p))


def _fee(price: float) -> float:
    return math.ceil(FEE_RATE * price * (1.0 - price) * 100.0) / 100.0


def _f(m: dict[str, Any], key: str) -> float | None:
    v = m.get(key)
    return float(v) if v is not None else None


def _ladder(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Two-sided strikes for one expiry, sorted ascending by strike."""
    rows = []
    for m in markets:
        strike = _f(m, "floor_strike")
        yb, ya = _f(m, "yes_bid_dollars"), _f(m, "yes_ask_dollars")
        na = _f(m, "no_ask_dollars")
        if strike is None or yb is None or ya is None or na is None:
            continue
        if yb <= 0.0 and ya >= 1.0:  # empty/one-sided wing quote — skip
            continue
        rows.append(
            {
                "strike": strike,
                "yes_bid": yb,
                "yes_ask": ya,
                "no_ask": na,
                "mid": (yb + ya) / 2.0,
                "yb_size": _f(m, "yes_bid_size_fp") or 0.0,
                "ya_size": _f(m, "yes_ask_size_fp") or 0.0,
            }
        )
    return sorted(rows, key=lambda r: r["strike"])


def _scan_expiry(label: str, lad: list[dict[str, Any]]) -> None:
    if len(lad) < 3:
        print(f"  {label}: only {len(lad)} two-sided strikes — skipping")
        return

    # 1) Monotonicity of the mid (informational).
    mono_viol = [
        (lad[i]["strike"], lad[i + 1]["strike"], lad[i + 1]["mid"] - lad[i]["mid"])
        for i in range(len(lad) - 1)
        if lad[i + 1]["mid"] > lad[i]["mid"] + 1e-9
    ]

    # 2) Executable risk-free arb: buy YES@K1 + NO@K2 (K1<K2) for < $1 net of fees.
    best: tuple[float, dict[str, Any], dict[str, Any]] | None = None
    n_arb = 0
    for i in range(len(lad)):
        for j in range(i + 1, len(lad)):
            lo, hi = lad[i], lad[j]  # strike(lo) < strike(hi)
            cost = lo["yes_ask"] + hi["no_ask"] + _fee(lo["yes_ask"]) + _fee(hi["no_ask"])
            net = 1.0 - cost  # combo pays >= $1 in every outcome
            if net > 0.0:
                n_arb += 1
                if best is None or net > best[0]:
                    best = (net, lo, hi)

    span = f"{lad[0]['strike']:,.0f}..{lad[-1]['strike']:,.0f}"
    print(
        f"  {label}: {len(lad)} two-sided strikes ({span})  "
        f"mid range {lad[0]['mid']:.2f}->{lad[-1]['mid']:.2f}"
    )
    print(f"    monotonicity (mid) violations: {len(mono_viol)}", end="")
    if mono_viol:
        worst = max(mono_viol, key=lambda v: v[2])
        print(f"  (worst +{worst[2] * 100:.1f}c at {worst[0]:,.0f}->{worst[1]:,.0f})")
    else:
        print()
    print(f"    EXECUTABLE risk-free arbs (net of fees): {n_arb}")
    if best is not None:
        net, lo, hi = best
        print(
            f"      best +{net * 100:.1f}c/contract: BUY YES@{lo['strike']:,.0f} "
            f"(ask {lo['yes_ask']:.2f}, sz {lo['ya_size']:.0f}) + "
            f"NO@{hi['strike']:,.0f} (ask {hi['no_ask']:.2f})"
        )


def main() -> int:
    load_dotenv()
    client = KalshiClient()
    try:
        for series in SERIES:
            markets = client.list_markets(series, status="open")
            by_exp: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for m in markets:
                by_exp[m["close_time"]].append(m)
            print(f"\n{series}: {len(markets)} open markets across {len(by_exp)} expiries")
            for exp in sorted(by_exp):
                _scan_expiry(exp, _ladder(by_exp[exp]))
    finally:
        client.close()

    print(
        "\nRead: 'EXECUTABLE risk-free arbs > 0' net of fees = a no-race static edge "
        "(verify\nliquidity + that quotes persist). All zero => the ladders are "
        "internally consistent\nwithin fees — efficient, like everything else here."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
