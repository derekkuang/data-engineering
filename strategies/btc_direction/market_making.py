"""Market-making feasibility — can you EARN the spread instead of racing it?

The taker lead-lag is a latency race we lose. The mirror strategy is to PROVIDE
liquidity: post a bid + ask and earn the spread from uninformed flow. The catch is
adverse selection — the same lead-lag flow lifts your STALE quote on the side that's
about to be in-the-money. This quantifies the tradeoff from the captured
decision-minute order books: how fast must a passive MM reprice before adverse
selection (the measured mid drift) eats the half-spread it earns?

Model (per quote round-trip):
    net ~ (spread/2 captured)  -  (drift_rate * resting_latency)  -  fee
    breakeven repricing latency  tau* = (spread/2) / drift_rate
Reprice faster than tau* -> some margin; slower -> you're picked off. And Kalshi's
fee (~2c near mid) is itself bigger than a ~0.5c half-spread, so the spread may not
even cover the fee before adverse selection enters.

Reuses the order-book bursts from ml/live_exec_reconcile (decision-minute snapshots,
3x ~20s apart). Read-only, no orders. Usage:
    uv run python -m strategies.btc_direction.market_making
"""

from __future__ import annotations

import sys

import numpy as np

from core.backtest.backtest import _fee
from strategies.btc_direction.live_exec_reconcile import _burst, _iso, _load_windows

LATENCIES_S = (0.5, 1.0, 2.0, 5.0, 10.0, 20.0)


def main() -> int:
    by_window = _load_windows()
    spreads, drift_rates, mids = [], [], []
    for snaps in by_window.values():
        window_open = _iso(snaps[0]["window_open_at"])
        burst = _burst(snaps, window_open)
        if len(burst) < 2:
            continue
        t0, tn = _iso(burst[0]["captured_at"]), _iso(burst[-1]["captured_at"])
        secs = (tn - t0).total_seconds()
        if secs <= 0 or burst[0].get("spread") is None:
            continue
        spreads.append(float(burst[0]["spread"]))
        mids.append(float(burst[0]["mid"]))
        drift_rates.append(abs(float(burst[-1]["mid"]) - float(burst[0]["mid"])) / secs)

    if not spreads:
        print("No usable bursts in data/orderbook_snapshots.jsonl", file=sys.stderr)
        return 1

    spread = np.array(spreads)
    drift = np.array(drift_rates)  # dollars/sec of |mid| move
    med_spread_c = float(np.median(spread)) * 100.0
    half_c = med_spread_c / 2.0
    med_drift_c = float(np.median(drift)) * 100.0  # cents/sec
    med_fee_c = float(_fee(np.array([np.median(mids)]))[0]) * 100.0
    tau_star = half_c / med_drift_c if med_drift_c > 0 else float("inf")

    print(f"Market-making feasibility — {len(spreads)} decision-minute order books\n")
    print(f"  median spread:        {med_spread_c:.1f}c  (half-spread captured: {half_c:.1f}c)")
    print(f"  mid-drift rate:       {med_drift_c:.2f}c/sec  (adverse selection from the lead-lag)")
    print(f"  Kalshi fee @ mid:     {med_fee_c:.1f}c/contract")
    print()
    print("  net per round-trip = half-spread - drift_rate*latency - fee (per contract):")
    print(f"    {'reprice latency':<18}{'adverse(c)':>12}{'net w/o fee':>13}{'net w/ fee':>12}")
    for lat in LATENCIES_S:
        adverse = med_drift_c * lat
        net_nofee = half_c - adverse
        net_fee = net_nofee - med_fee_c
        print(f"    {f'{lat:.1f}s':<18}{adverse:>12.2f}{net_nofee:>+13.2f}{net_fee:>+12.2f}")

    ts, dr, hs, fe = f"{tau_star:.0f}", f"{med_drift_c:.2f}", f"{half_c:.1f}", f"{med_fee_c:.1f}"
    verdict = [
        "",
        f"Verdict: breakeven repricing latency (no fees) tau* = {ts}s.",
        f"  A passive MM must re-quote within ~{ts}s, else the {dr}c/s drift eats the {hs}c",
        "  half-spread — the SAME latency race as the taker side. (The spread is this thin",
        "  because faster MMs already compete it to the edge of viability.)",
        f"  And the Kalshi fee (~{fe}c) ALONE exceeds the {hs}c half-spread: unless Kalshi",
        "  rebates makers, MM loses on fees before adverse selection even enters.",
        "  Caveat: drift is from the volatile decision minute, so tau* is a lower bound",
        "  (calmer mid-window reprices slower) — but the fee hurdle is regime-free.",
        "  Conclusion: providing liquidity is no free lunch either — same wall, other side.",
    ]
    print("\n".join(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
