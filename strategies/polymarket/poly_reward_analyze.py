"""Analyze the Polymarket reward logs into the ONE decisive number: does the liquidity
reward pool beat the pick-off it compensates?

Read data/poly_reward_book.csv + data/poly_reward_trades.csv
(from strategies.polymarket.poly_reward_logger).

THE KEY INSIGHT that makes this capital-independent. Resting two-sided near the mid earns
you, to first order, a share s = your_size / (your_size + competing_depth) of BOTH:
  * the daily reward pool   (reward  ~ pool_rate x s)
  * the adverse selection   (pick-off ~ total_maker_markout x s)
because reward credit and fill probability both scale with your resting size. So s CANCELS
in the comparison, and the maker is collectively profitable iff:

        daily reward POOL  >  daily total maker PICK-OFF (markout dollars)

That is measurable entirely from public data: the pool from the reward config, the pick-off
as the markout that the resting side (the makers) realizes against every public taker print.
maker_markout_per_share = -taker_sign x (mid[t+h] - mid[t]), summed x size, annualized to
/day. > 0 means resting makers get PAID by flow (benign); < 0 is pick-off the reward must
cover. taker_sign is normalized to the token0 (Over/Yes/Up) price space the book mid is in.

Capital only enters second-order: min_size sets a floor (~$400-1000 two-sided here) and your
absolute $ = s x (pool - pick-off). The ratio decides go/no-go; capital decides whether the
absolute number clears the crypto/KYC friction.

Usage:
    uv run python -m strategies.polymarket.poly_reward_analyze
    uv run python -m strategies.polymarket.poly_reward_analyze --horizons 30,60,120
"""

from __future__ import annotations

import argparse
import bisect
import csv
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

BOOK_LOG = "data/poly_reward_book.csv"
TRADE_LOG = "data/poly_reward_trades.csv"


@dataclass
class Market:
    question: str = ""
    rate: float = 0.0  # reward pool $/day (last seen)
    ts: list[float] = field(default_factory=list)  # book tick unix times (sorted)
    mid: list[float] = field(default_factory=list)  # token0 mid aligned with ts


def _iso_to_unix(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def load_books(path: str) -> dict[str, Market]:
    """condition_id -> Market with a sorted (ts, mid) series in token0 price space."""
    mkts: dict[str, Market] = defaultdict(Market)
    p = Path(path)
    if not p.exists():
        return {}
    with p.open() as fh:
        for row in csv.DictReader(fh):
            cid = row["condition_id"]
            m = mkts[cid]
            m.question = row["question"]
            m.rate = float(row["reward_rate"])
            m.ts.append(_iso_to_unix(row["ts_utc"]))
            m.mid.append(float(row["mid"]))
    for m in mkts.values():  # ensure monotonic ts for bisect
        order = sorted(range(len(m.ts)), key=lambda i: m.ts[i])
        m.ts = [m.ts[i] for i in order]
        m.mid = [m.mid[i] for i in order]
    return dict(mkts)


def mid_asof(m: Market, t: float) -> float | None:
    """Token0 mid at or just before time t (None if outside the logged window)."""
    if not m.ts or t < m.ts[0] or t > m.ts[-1]:
        return None
    i = bisect.bisect_right(m.ts, t) - 1
    return m.mid[i] if i >= 0 else None


@dataclass
class Fill:
    cid: str
    size: float
    markout: float  # maker markout per share in token0 dollars (>0 maker profits)


def score_trades(books: dict[str, Market], path: str, horizon: float) -> list[Fill]:
    """Maker markout for every public taker print that falls inside the book window,
    normalized so a positive markout means the RESTING (maker) side profited."""
    fills: list[Fill] = []
    p = Path(path)
    if not p.exists():
        return fills
    with p.open() as fh:
        for row in csv.DictReader(fh):
            cid = row["condition_id"]
            m = books.get(cid)
            if m is None:
                continue
            try:
                t = float(row["trade_ts"])
                size = float(row["size"])
                oidx = int(row["outcome_index"])
            except (TypeError, ValueError):
                continue
            m0 = mid_asof(m, t)
            m1 = mid_asof(m, t + horizon)
            if m0 is None or m1 is None:
                continue
            # taker sign in token0 (Over/Yes) space: BUY token0 = +1; a trade on token1
            # (Under/No) flips. maker holds the opposite, so maker_markout = -sign x dmid.
            side = 1.0 if row["side"] == "BUY" else -1.0
            taker_sign = side * (1.0 if oidx == 0 else -1.0)
            fills.append(Fill(cid, size, -taker_sign * (m1 - m0)))
    return fills


def block_bootstrap_cents(
    fills: list[Fill], n_boot: int = 5000, seed: int = 0
) -> tuple[float, float]:
    """95% CI on the size-weighted mean maker markout (cents/share), resampling whole
    MARKETS with replacement (pick-off clusters within a game, so markets are the block)."""
    by_mkt: dict[str, list[Fill]] = defaultdict(list)
    for f in fills:
        by_mkt[f.cid].append(f)
    blocks = list(by_mkt.values())
    if len(blocks) < 2:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_boot):
        sample = [f for _ in blocks for f in rng.choice(blocks)]
        num = sum(f.markout * f.size for f in sample)
        den = sum(f.size for f in sample)
        if den:
            means.append(100.0 * num / den)
    means.sort()
    return means[int(0.025 * len(means))], means[int(0.975 * len(means))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", type=str, default="60,120", help="markout horizons, seconds")
    args = ap.parse_args()
    horizons = [float(x) for x in args.horizons.split(",")]

    books = load_books(BOOK_LOG)
    if not books:
        print(f"No book data at {BOOK_LOG} — run strategies.polymarket.poly_reward_logger first.")
        return 1

    all_ts = [t for m in books.values() for t in m.ts]
    span_days = (max(all_ts) - min(all_ts)) / 86400.0 if len(all_ts) > 1 else 0.0
    pool_day = sum(m.rate for m in books.values())

    print("=" * 78)
    print("POLYMARKET REWARD vs PICK-OFF")
    print("=" * 78)
    print(f"Markets: {len(books)}   book ticks: {len(all_ts):,}   "
          f"session span: {span_days * 24:.2f}h")
    print(f"Total reward pool: ${pool_day:,.0f}/day  (sum of tracked markets' daily rates)")
    if span_days < 0.02:
        print("\n** Session too short to annualize pick-off meaningfully — this is a MECHANICS")
        print("   check only. Log a full game (--minutes 180+) for a real verdict. **")

    for h in horizons:
        fills = score_trades(books, TRADE_LOG, h)
        if not fills:
            print(f"\n[h={h:.0f}s] no trades inside the book window.")
            continue
        vol = sum(f.size for f in fills)
        markout_sum = sum(f.markout * f.size for f in fills)  # token0 $; >0 = maker profits
        wavg_c = 100.0 * markout_sum / vol if vol else 0.0
        lo, hi = block_bootstrap_cents(fills)
        # WINDOW-MATCHED (no 24/7 annualization — pick-off only happens during live games,
        # so extrapolating the game-hour flow to a full day overstates it). Compare the
        # reward EARNED over the logged window to the pick-off SUFFERED over the same window.
        pickoff_win = -markout_sum  # $ the resting side lost over the window (>0 = loss)
        reward_win = pool_day * span_days  # reward accrued over the window at 100% share
        cover = 100.0 * reward_win / pickoff_win if pickoff_win > 0 else float("inf")

        print(f"\n--- horizon {h:.0f}s -------------------------------------------------------")
        print(f"  taker prints scored : {len(fills):,}   shares: {vol:,.0f}")
        print(f"  maker markout       : {wavg_c:+.3f} c/share   "
              f"[market-block 95% CI {lo:+.3f}, {hi:+.3f}]")
        print("    (>0 = resting side PAID by flow; <0 = pick-off the reward must cover)")
        print(f"  pick-off (window)   : ${pickoff_win:,.0f}   over {span_days * 24:.1f}h of flow")
        print(f"  reward   (window)   : ${reward_win:,.2f}   (pool ${pool_day:,.0f}/day x window)")
        print(f"  REWARD COVERS {cover:.1f}% of pick-off   "
              f"=> {'farm it' if cover > 100 else 'SUBSIDY << toxicity (walk)'}")

    # per-market detail at the first horizon
    h0 = horizons[0]
    fills0 = score_trades(books, TRADE_LOG, h0)
    by_mkt: dict[str, list[Fill]] = defaultdict(list)
    for f in fills0:
        by_mkt[f.cid].append(f)
    print(f"\nPER-MARKET (horizon {h0:.0f}s)")
    print(f"  {'pool/d':>7} {'fills':>6} {'shares':>10} {'markout_c':>10}  market")
    print("  " + "-" * 74)
    rows = []
    for cid, fs in by_mkt.items():
        v = sum(f.size for f in fs)
        mc = 100.0 * sum(f.markout * f.size for f in fs) / v if v else 0.0
        rows.append((books[cid].rate, len(fs), v, mc, books[cid].question))
    for rate, n, v, mc, q in sorted(rows, reverse=True)[:20]:
        print(f"  {rate:>7.0f} {n:>6} {v:>10,.0f} {mc:>+10.3f}  {q[:44]}")

    print("\nVERDICT FRAME: pool > pick-off => makers collectively profit and your share")
    print("scales with deployed size; pool < pick-off => the reward is a toxicity subsidy.")
    print("First-order (reward share ~ fill share); min_size sets a ~$400-1000 capital floor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
