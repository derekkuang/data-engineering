"""Passive Polymarket reward-market logger — captures the tick series needed to answer
the ONE question that decides reward farming: does the liquidity-reward income beat the
goal/round pick-off, at sane capital?

Polymarket pays makers a LIQUIDITY REWARD: daily USDC for resting two-sided near the mid,
paid even if UNFILLED, as long as your quote sits within the market's `max_spread` band.
The catch we already measured with the navigator: modeled gross yields are absurd
(260-1494% APR) because the reward COMPENSATES adverse selection — these are jumpy in-play
totals/handicaps (CS2 rounds, soccer goals, MLB innings) that lurch every event, the same
pick-off that killed tennis market-making, except here you're PAID to absorb it. A snapshot
can't tell us whether reward > pick-off; only the time series through a live game can.

This is the read-only opposite of ml/lp_live — it places NO orders, uses NO auth, needs NO
KYC. It is the Polymarket analog of ml/tennis_logger. For every reward-eligible,
mean-reverting, in-band market with a live book it appends two logs:
  * data/poly_reward_book.csv   one row per market per poll: bid/ask/mid/spread, top sizes,
    competing depth inside the reward band, and the reward config (daily rate, band, min_size)
  * data/poly_reward_trades.csv one row per public trade print: asset/outcome, side, price,
    size, exchange timestamp

From these ml/poly_reward_analyze.py later computes realized markout (pick-off) per resting
share and compares it to the modeled reward income on the same axis — all BEFORE risking a
cent (or the KYC/iOS/USDC friction). reward/day > pick-off/day => build it; else it's a
subsidy that merely offsets toxicity => walk. See the LP market-making track notes.

Usage:
    uv run python -m ml.research.poly_reward_logger --minutes 180          # log for 3h
    uv run python -m ml.research.poly_reward_logger --minutes 600 --poll 3  # overnight
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ml.research.polymarket_navigator import USDC, fetch_reward_markets, is_meanrev

CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
BOOK_LOG = "data/poly_reward_book.csv"
TRADE_LOG = "data/poly_reward_trades.csv"

MIN_MID, MAX_MID = 0.05, 0.92  # same maker band as Kalshi: away from one-sided/resolving books
MAX_SPREAD_LIVE = 0.10  # a wider book than this is a dead/pre-game market, not a live one
MIN_RATE = 1.0  # USDC/day reward floor — skip dust pools
LIVE_WINDOW_S = 300.0  # a market is "live" if it printed a trade in the last 5 min
MIN_LIVE_TRADES = 3  # require this many recent prints — pick-off only happens where flow is
DEFAULT_POLL = 4.0  # seconds between book polls of each tracked market
REFRESH_S = 90.0  # re-scan the reward universe this often (markets resolve, games start)
DEFAULT_MAX = 25  # cap tracked markets (highest reward pool first) to bound API load

BOOK_HDR = [
    "ts_utc", "condition_id", "token_id", "outcome", "question",
    "bid", "ask", "mid", "spread_c", "bid_sz", "ask_sz",
    "depth_bid_band", "depth_ask_band", "reward_rate", "band_c", "min_size",
]
TRADE_HDR = [
    "ts_utc", "condition_id", "asset", "outcome", "outcome_index",
    "side", "price", "size", "trade_ts", "tx_hash",
]


class Target:
    """One reward-eligible mean-reverting market we are tracking (token0 = reference outcome)."""

    __slots__ = ("condition_id", "token_id", "outcome", "question", "rate", "band", "min_size")

    def __init__(self, m: dict[str, Any], rate: float) -> None:
        tok0 = m["tokens"][0]
        rw = m.get("rewards") or {}
        self.condition_id = str(m.get("condition_id"))
        self.token_id = str(tok0["token_id"])
        self.outcome = str(tok0.get("outcome", ""))
        self.question = str(m.get("question", ""))
        self.rate = rate
        self.band = float(rw.get("max_spread") or 3) / 100.0  # cents -> dollars
        self.min_size = float(rw.get("min_size") or 0)


def usdc_rate(m: dict[str, Any]) -> float:
    rw = m.get("rewards") or {}
    return sum(
        float(x.get("rewards_daily_rate") or 0)
        for x in (rw.get("rates") or [])
        if str(x.get("asset_address", "")).lower() == USDC
    )


def top_of_book(book: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """(best_bid, best_ask, bid_size, ask_size) for this token, or None if one-sided."""
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return None
    bb = max(bids, key=lambda o: float(o["price"]))
    ba = min(asks, key=lambda o: float(o["price"]))
    return float(bb["price"]), float(ba["price"]), float(bb["size"]), float(ba["size"])


def depth_in_band(book: dict[str, Any], mid: float, band: float) -> tuple[float, float]:
    """Competing resting size within `band` of mid on each side — our reward share is
    ~ deploy / (deploy + this). Aggregated levels, so it includes sub-min_size orders."""
    bid_d = sum(float(o["size"]) for o in (book.get("bids") or [])
                if 0 <= mid - float(o["price"]) <= band)
    ask_d = sum(float(o["size"]) for o in (book.get("asks") or [])
                if 0 <= float(o["price"]) - mid <= band)
    return bid_d, ask_d


def recent_trade_count(client: httpx.Client, condition_id: str, now: float) -> int:
    """How many public prints this market had in the last LIVE_WINDOW_S — our liveness
    signal. Pick-off can only be measured where takers are actually hitting the book; a
    deep tight book with no recent flow is a pre-game/finished market, not a live game."""
    try:
        r = client.get(f"{DATA}/trades", params={"market": condition_id, "limit": 100})
        r.raise_for_status()
        trades = r.json()
    except Exception:
        return 0
    if not isinstance(trades, list):
        return 0
    cutoff = now - LIVE_WINDOW_S
    return sum(1 for t in trades if float(t.get("timestamp") or 0) >= cutoff)


def reward_targets(client: httpx.Client, cap: int) -> list[Target]:
    """Reward-eligible, mean-reverting, in-band markets that are TRADING NOW, most active
    first (capped). Dead/pre-game books (deep + tight but no recent flow) are dropped — the
    'uncontested pool' on a market with no flow is a mirage. Returns [] when nothing is live
    (e.g. overnight, no games) so the logger idles instead of polling dead books."""
    cands: list[tuple[float, dict[str, Any]]] = []
    for m in fetch_reward_markets(client):
        rate = usdc_rate(m)
        if rate < MIN_RATE or not is_meanrev(str(m.get("question", "")), []) or not m.get("tokens"):
            continue
        cands.append((rate, m))
    cands.sort(reverse=True, key=lambda x: x[0])  # check the richest pools first

    now = time.time()
    scored: list[tuple[int, dict[str, Any], float]] = []  # (recent_trades, market, rate)
    for rate, m in cands:
        if len(scored) >= cap * 3:  # bound API load: only probe the top reward candidates
            break
        try:
            book = client.get(f"{CLOB}/book", params={"token_id": str(m["tokens"][0]["token_id"])})
            book.raise_for_status()
            tob = top_of_book(book.json())
        except Exception:
            continue
        if not tob:
            continue
        bid, ask, _, _ = tob
        mid, spread = (bid + ask) / 2, ask - bid
        if not (MIN_MID <= mid <= MAX_MID) or spread > MAX_SPREAD_LIVE:
            continue  # one-sided / resolving / dead pre-game -> not a live reward market
        n_recent = recent_trade_count(client, str(m.get("condition_id")), now)
        if n_recent >= MIN_LIVE_TRADES:
            scored.append((n_recent, m, rate))

    scored.sort(reverse=True, key=lambda x: x[0])  # most active first
    return [Target(m, rate) for _, m, rate in scored[:cap]]


def _append(path: str, header: list[str], rows: list[list[Any]]) -> None:
    if not rows:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(header)
        w.writerows(rows)


def _poll_market(
    client: httpx.Client, tgt: Target, now_iso: str, seen: set[str]
) -> tuple[list[Any] | None, list[list[Any]]]:
    """Return (book_row_or_None, new_trade_rows) for one market this cycle."""
    book_row: list[Any] | None = None
    try:
        book = client.get(f"{CLOB}/book", params={"token_id": tgt.token_id})
        book.raise_for_status()
        b = book.json()
        tob = top_of_book(b)
    except Exception:
        tob = None
    if tob:
        bid, ask, bsz, asz = tob
        mid = (bid + ask) / 2
        d_bid, d_ask = depth_in_band(b, mid, tgt.band)
        book_row = [
            now_iso, tgt.condition_id, tgt.token_id, tgt.outcome, tgt.question,
            f"{bid:.4f}", f"{ask:.4f}", f"{mid:.4f}", f"{(ask - bid) * 100:.2f}",
            f"{bsz:.2f}", f"{asz:.2f}", f"{d_bid:.2f}", f"{d_ask:.2f}",
            f"{tgt.rate:.2f}", f"{tgt.band * 100:.2f}", f"{tgt.min_size:.0f}",
        ]

    trade_rows: list[list[Any]] = []
    try:
        raw = client.get(f"{DATA}/trades", params={"market": tgt.condition_id, "limit": 100})
        raw.raise_for_status()
        trades = raw.json()
    except Exception:
        trades = []
    for tr in trades if isinstance(trades, list) else []:
        # no stable trade id on the public feed -> composite key (a wallet rarely repeats an
        # identical fill within one tx at the same price/size/second)
        key = "|".join(str(tr.get(k)) for k in
                       ("transactionHash", "asset", "side", "price", "size", "timestamp"))
        if key in seen:
            continue
        seen.add(key)
        trade_rows.append([
            now_iso, tgt.condition_id, tr.get("asset"), tr.get("outcome"),
            tr.get("outcomeIndex"), tr.get("side"), tr.get("price"), tr.get("size"),
            tr.get("timestamp"), tr.get("transactionHash"),
        ])
    return book_row, trade_rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=180.0)
    ap.add_argument("--poll", type=float, default=DEFAULT_POLL)
    ap.add_argument("--max-markets", type=int, default=DEFAULT_MAX)
    args = ap.parse_args()

    client = httpx.Client(timeout=20.0, headers={"User-Agent": "de-portfolio-research/1.0"})
    end = time.time() + args.minutes * 60
    seen: dict[str, set[str]] = {}  # condition_id -> logged trade keys
    targets: list[Target] = []
    last_refresh = 0.0
    n_book = n_trades = 0
    print(f"Poly reward logger: {args.minutes:.0f}min, poll {args.poll:.0f}s, "
          f"<={args.max_markets} mkts")
    print(f"READ-ONLY (no orders, no auth, no KYC) -> {BOOK_LOG} + {TRADE_LOG}. Ctrl-C to stop.\n")
    try:
        while time.time() < end:
            t0 = time.time()
            if time.time() - last_refresh >= REFRESH_S:
                try:
                    targets = reward_targets(client, args.max_markets)
                    last_refresh = time.time()
                    pool = sum(t.rate for t in targets)
                    print(f"  [{datetime.now(UTC):%H:%M:%S}] tracking {len(targets)} reward mkts "
                          f"(${pool:,.0f}/day total pool)")
                except Exception as exc:
                    print(f"  refresh error: {str(exc)[:80]}")
            now_iso = datetime.now(UTC).isoformat(timespec="seconds")
            book_rows: list[list[Any]] = []
            trade_rows: list[list[Any]] = []
            for tgt in targets:
                try:
                    seen_tgt = seen.setdefault(tgt.condition_id, set())
                    br, trs = _poll_market(client, tgt, now_iso, seen_tgt)
                    if br:
                        book_rows.append(br)
                    trade_rows.extend(trs)
                except Exception as exc:
                    print(f"  {tgt.question[:30]} poll error: {str(exc)[:60]}")
            _append(BOOK_LOG, BOOK_HDR, book_rows)
            _append(TRADE_LOG, TRADE_HDR, trade_rows)
            n_book += len(book_rows)
            n_trades += len(trade_rows)
            time.sleep(max(0.0, args.poll - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\nstopped early.")
    finally:
        client.close()
    print(f"\nDONE: {n_book} book ticks + {n_trades} trades logged -> {BOOK_LOG}, {TRADE_LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
