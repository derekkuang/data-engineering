"""LP pilot — PHASE B: LIVE inventory-managed maker on Kalshi (REAL MONEY).

This places REAL orders. It is the validated paper v2 control logic (ml/lp_pilot)
with the simulated fill swapped for a real post-only resting order. Same ultra-small
caps: +/-MAX_POSITION contracts, $DAILY_LOSS_LIMIT mark-to-mid kill switch.

WHY A STAGED LADDER. The control logic is validated on paper, but the live API
binding (exact create-order path/payload, positions/balance shapes, list path) can
only be confirmed against the real authenticated API. A wrong binding fails as a
REJECTED order (HTTP 4xx), not an unintended trade — it fails safe. The three modes
climb the ladder with ~zero risk before any real quoting:

  --auth-check : GET balance + positions (read-only). Proves creds + reveals shapes.
  --test-order : place ONE post-only bid at $0.02 (cannot fill — far below any market),
                 read it back, cancel it. Proves create+list+cancel+signing end-to-end.
                 Max risk if it somehow filled: $0.02.
  --live       : the real quoting loop. REQUIRES --i-understand-live in addition.

Run them IN ORDER. If --auth-check or --test-order reveal a field/path mismatch,
fix the constant below before going live. Order/price conventions (V2): side is
'bid'/'ask' (YES-leg), price is a dollar string, post_only=True = maker-only.

Usage:
    uv run python -m ml.lp.lp_live --auth-check
    uv run python -m ml.lp.lp_live --test-order              # auto-picks a market
    uv run python -m ml.lp.lp_live --live --i-understand-live --minutes 15
    uv run python -m ml.lp.lp_live --live --i-understand-live --prefix KXWC --ws  # WS-fed book

WebSocket mode (--live --ws): runs THIS SAME strategy but sources market data over the WS
(ingestion/kalshi_ws via ml/lp/ws_book_feed) instead of REST polling — the per-poll order
book (orderbook_delta) AND our own fills (private `fill` channel, drained each poll). Order
PLACEMENT and CANCELLATION stay REST: Kalshi has no WS order entry. Fills are reconciled
against REST /portfolio/fills every FILL_RECONCILE_S (and forced on exit) because the fill
channel has no seq numbers — REST stays authoritative, WS just lowers latency. Single-market:
it feeds whichever one market is being quoted. The multi-market WS maker is a larger build.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from ingestion.kalshi import INLINE_KEY_ENV_VARS, KalshiClient
from ml.lp.lp_pilot import (
    DAILY_LOSS_LIMIT,
    MAX_MID,
    MAX_POSITION,
    MIN_MID,
    POLL_SECONDS,
    best_bid_ask,
    better_market,
    pick_smooth_ticker,
)
from ml.lp.ws_book_feed import WsBookFeed

# V2 order endpoints (confirm via --test-order before --live; change here if needed).
ORDER_PATH = "/portfolio/events/orders"  # POST create, DELETE {id} cancel
ORDER_LIST_PATH = "/portfolio/orders"  # GET resting orders
SAFE_TEST_PRICE = 0.02  # a bid here cannot fill on a real market -> validates plumbing

# Inventory skew: dollars to push the ACCUMULATING-side quote off the touch per
# contract of inventory, so inventory mean-reverts to flat (the fix for the +6
# directional drift). 0.01 = pull the bid 1c lower for each long contract held.
SKEW_PER_CONTRACT = 0.01
QUOTE_SIZE = 2  # contracts posted per side per poll (was 1). The scale lever — the cap was
#                 never binding (held +/-2-3 of 10), so size, not the cap, is what scales us.
MARKOUT_HORIZON_S = 30  # seconds; the edge signal = did mid move against us post-fill
# WS-fills (--ws): our executions arrive on the private `fill` channel (low latency) BUT it
# carries no seq numbers, so a dropped message is undetectable. REST /portfolio/fills stays the
# AUTHORITATIVE source — polled this often as a reconcile, and FORCED on market exit before we
# flatten. trade_id dedup makes WS+REST overlap harmless, so a WS hiccup degrades to REST latency
# (never wrong inventory). Bounds any WS-missed-fill inventory error to this window.
FILL_RECONCILE_S = 12.0
DEAD_BOOK_POLLS = 8  # consecutive one-sided/empty polls -> market resolved, end session
NO_FILL_TIMEOUT_S = 240  # no fills this long -> leave the (illiquid/dud) market and roll
# ABANDON THIN-AND-NOT-PAYING: low realized FLOW (not just zero fills) usually doesn't pay.
# Calibrated on 432 logged markets: the <3 fills/min bucket nets ~$0 over 243 markets while
# the edge is entirely in >=3 fpm (mid 3-6: +$10/101 markets, busy >=6: +$45/88). BUT a thin
# market still earning our target (>=GOOD_RATE $/min) is KEPT — abandon needs BOTH thin AND
# not-paying. Unlike the no-fill timeout (needs ZERO fills) and the flat-only switch, this
# fires while we're HOLDING a slow-bleeding position — the thin-grind "losing battle".
THIN_WINDOW_S = 180  # trailing window over which realized fills/min is measured
THIN_FPM_FLOOR = 3.0  # below this many fills/min (after warmup) -> too thin; abandon
THIN_WARMUP_S = 120  # give a fresh market this long to show flow before judging it thin
# CHASE FLOW: while quoting, periodically scan for a more active market and switch to it
# — but ONLY while flat (so leaving costs no flatten + no half-captured spread). The
# activity bar is PERFORMANCE-AWARE: because we only switch while flat, our P&L then ≈
# realized cash, so the cash gained since the last scan = $/min this market is paying us.
# If it's paying (>=GOOD_RATE) we get STICKY (demand a much busier candidate before we
# give up queue position); if we're flat/bleeding we get EAGER (leave for a modest edge).
SWITCH_SCAN_S = 45  # seconds between "is a hotter market available?" scans
MIN_DWELL_S = 60  # stay in a market at least this long before a switch is allowed
GOOD_RATE = 0.01  # $/min realized; at/above this the current market is "paying" -> sticky
STICKY_FACTOR = 3.0  # while earning, only switch to a candidate >= this x as active
EAGER_FACTOR = 1.5  # while flat/bleeding, switch to a candidate >= this x as active
# MIN_MID/MAX_MID (the quote/hold price band) are imported from lp_pilot so entry
# (pick_smooth_ticker) and the "extreme" exit here use ONE definition — see there.
FLAT_EPS = 0.005  # |inventory| below this = flat (markets allow fractional fills)
SESSION_LOG = "data/lp_sessions.csv"  # one row per run (accumulate the sample)
FILL_LOG = "data/lp_fills.csv"  # one row per fill with its markout
# Stamped into every logged session so analysis can separate runs by ruleset and not
# blend incompatible regimes. BUMP this (new dated tag) whenever a selection/quoting
# rule changes materially. Rows logged before this column existed read back as "legacy".
#   2026-06-17-gated : ITF excluded + recent-trade floor 15 + flow-switch + band 0.05/no-cap
#   2026-06-18-tuned : + upper cap 0.92, prefer mean-reverting over trending (GAME/WINNER)
#                      markets, exclude MENTION/SOA props, performance-aware switch bar
#   2026-06-18-meanrev: TOTAL/SPREAD ONLY (allowlist) — never quote winner/moneyline/match
#                      or directional props; idle if none active (fix for -$5 overnight ATP)
#   2026-06-21-2x    : same selection, but QUOTE_SIZE 1->2, cap 10->20, kill $5->$10 — the
#                      2x scale test (compare per-fill capture + markout vs the 1x baseline)
#   2026-06-22-thin  : + flow-based abandon — leave (flatten+roll) a market only when BOTH
#                      fills/min < THIN_FPM_FLOOR AND mark-P&L rate < GOOD_RATE (thin AND not
#                      paying), after warmup, EVEN WHILE HOLDING (fixes the thin-grind "losing
#                      battle" but keeps thin-but-paying markets). Still 2x size.
CONFIG_VERSION = "2026-06-22-thin"


def _need_auth(client: KalshiClient) -> None:
    if client.authenticated:
        return
    # Diagnostic (presence only — never prints secret values).
    key_id = bool(os.environ.get("KALSHI_API_KEY_ID", "").strip())
    inline = {v: bool(os.environ.get(v, "").strip()) for v in INLINE_KEY_ENV_VARS}
    path = bool(os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip())
    print("FAIL: not authenticated. Need BOTH a key id AND a private key.", file=sys.stderr)
    print(f"  KALSHI_API_KEY_ID set:        {key_id}", file=sys.stderr)
    print(f"  inline key var set:           {inline}", file=sys.stderr)
    print(f"  KALSHI_PRIVATE_KEY_PATH set:  {path}", file=sys.stderr)
    print(
        "  Fix: in .env set KALSHI_API_KEY_ID=<id> and paste the PEM as "
        "KALSHI_PRIVATE_KEY (double-quoted multiline).",
        file=sys.stderr,
    )
    raise SystemExit(2)


def place_order(
    client: KalshiClient,
    ticker: str,
    side: str,
    price: float,
    count: float,
    *,
    post_only: bool,
    ioc: bool = False,
) -> dict[str, Any]:
    """side='bid' buys YES, 'ask' sells YES. price in dollars, count in contracts
    (fractional allowed). post_only=maker-only."""
    body: dict[str, Any] = {
        "ticker": ticker,
        "client_order_id": uuid.uuid4().hex,
        "side": side,
        "count": f"{count:.2f}",
        "price": f"{price:.4f}",
        "time_in_force": "immediate_or_cancel" if ioc else "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": post_only,
    }
    return client.post(ORDER_PATH, body)


def _order_id(resp: dict[str, Any]) -> str | None:
    o = resp.get("order") or resp
    oid = o.get("order_id") or o.get("id")
    return str(oid) if oid else None


def resting_orders(client: KalshiClient, ticker: str | None = None) -> list[dict[str, Any]]:
    params = {"status": "resting"}
    if ticker:
        params["ticker"] = ticker
    resp = client.get(ORDER_LIST_PATH, params=params)
    orders: list[dict[str, Any]] = resp.get("orders", [])
    return orders


def cancel_all(client: KalshiClient, ticker: str | None = None) -> int:
    n = 0
    for o in resting_orders(client, ticker):
        oid = o.get("order_id") or o.get("id")
        if not oid:
            continue
        try:
            client.delete(f"{ORDER_PATH}/{oid}")
            n += 1
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:  # 404 = already gone/filled = benign
                print(f"  cancel {oid} failed: {str(exc)[:80]}")
        except Exception as exc:
            print(f"  cancel {oid} failed: {str(exc)[:80]}")
    return n


def fetch_fills(client: KalshiClient, ticker: str) -> list[dict[str, Any]]:
    resp = client.get("/portfolio/fills", params={"ticker": ticker, "limit": 200})
    fills: list[dict[str, Any]] = resp.get("fills", [])
    return fills


def _fill_price(f: dict[str, Any]) -> float:
    if f.get("yes_price_dollars") is not None:
        return float(f["yes_price_dollars"])
    if f.get("yes_price") is not None:
        return float(f["yes_price"]) / 100.0  # cents fallback
    return 0.0


def _fill_count(f: dict[str, Any]) -> float:
    """Fill size in contracts — FRACTIONAL (markets allow partial fills like 0.36);
    truncating to int silently drops partials and leaves un-flattened residuals."""
    return float(f.get("count_fp") or f.get("count") or 0)


def _fill_ts(f: dict[str, Any]) -> float:
    """Fill exchange time in epoch SECONDS. REST fills carry `ts` (seconds); WS `fill` msgs
    carry `ts_ms` (milliseconds) — normalize both so markouts line up regardless of source."""
    if f.get("ts") is not None:
        return float(f["ts"])
    if f.get("ts_ms") is not None:
        return float(f["ts_ms"]) / 1000.0
    return time.time()


def position(client: KalshiClient, ticker: str) -> int:
    """Net signed YES contracts in this market (long +, short -). Kalshi reports it
    as ``position_fp`` (a fixed-point string) under market_positions."""
    resp = client.get("/portfolio/positions", params={"ticker": ticker})
    for p in resp.get("market_positions", resp.get("positions", [])):
        if p.get("ticker") == ticker:
            return int(float(p.get("position_fp") or p.get("position") or 0))
    return 0


def balance_dollars(client: KalshiClient) -> float:
    resp = client.get("/portfolio/balance")
    return float(resp.get("balance", 0)) / 100.0  # Kalshi returns cents


def account_pnl(client: KalshiClient, ticker: str) -> tuple[float, float]:
    """(realized_pnl, fees_paid) for the market from Kalshi's own books — the
    authoritative cross-check on our fills-based cash."""
    resp = client.get("/portfolio/positions", params={"ticker": ticker})
    for p in resp.get("market_positions", []):
        if p.get("ticker") == ticker:
            return float(p.get("realized_pnl_dollars") or 0), float(p.get("fees_paid_dollars") or 0)
    return 0.0, 0.0


def _append_csv(path: str, header: list[str], row: list[Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(header)
        w.writerow(row)


# --- modes ------------------------------------------------------------------
def auth_check(client: KalshiClient) -> int:
    _need_auth(client)
    print(f"balance: ${balance_dollars(client):.2f}")
    resp = client.get("/portfolio/positions")
    pos = resp.get("market_positions", resp.get("positions", []))
    print(f"open positions: {len(pos)}")
    for p in pos[:10]:
        print(f"  {p.get('ticker')}: {p.get('position')}")
    print("\nAuth OK. Next: --test-order")
    return 0


def test_order(client: KalshiClient, ticker: str | None) -> int:
    _need_auth(client)
    tk = ticker or pick_smooth_ticker(client)
    if not tk:
        print("No market to test on; pass --ticker.")
        return 1
    print(f"Placing ONE post-only bid: 1 @ ${SAFE_TEST_PRICE:.2f} on {tk} (cannot fill) ...")
    try:
        resp = place_order(client, tk, "bid", SAFE_TEST_PRICE, 1, post_only=True)
    except Exception as exc:
        print(f"\nORDER REJECTED: {str(exc)[:200]}")
        print("=> binding mismatch (path/payload). Fix ORDER_PATH / payload, retry.")
        return 1
    oid = _order_id(resp)
    print(f"  placed. order_id={oid}")
    found = [o for o in resting_orders(client, tk) if (o.get("order_id") or o.get("id")) == oid]
    print(f"  appears in resting orders: {bool(found)}")
    n = cancel_all(client, tk)
    print(f"  cancelled {n} order(s).")
    print("\nPlumbing validated end-to-end (create + list + cancel + signing). Ready for --live.")
    return 0


def _run_market(
    client: KalshiClient,
    tk: str,
    end: float,
    poll: float,
    debug: bool,
    retired: set[str],
    allow_switch: bool,
    quote_size: int = QUOTE_SIZE,
    config_tag: str = CONFIG_VERSION,
    prefixes: tuple[str, ...] | None = None,
    feed: WsBookFeed | None = None,
) -> tuple[str, float, str | None]:
    """Quote one market until it resolves (dead book), the kill switch trips, the flow
    dries up (thin/illiquid), a much more active market appears (while we're flat), or
    the session clock `end` is reached. Flattens + logs on exit. Returns (reason, pnl,
    next_ticker) where reason in {'dead','kill','extreme','thin','illiquid','switch',
    'time'}; next_ticker is the market
    to switch into (only when reason=='switch', else None). `retired` is excluded from
    switch candidates; `allow_switch` gates the flow-chasing scan."""
    cancel_all(client, tk)

    our_orders: dict[str, str] = {}  # order_id -> 'bid'/'ask' (so a fill -> buy/sell yes)
    seen: set[str] = {f["trade_id"] for f in fetch_fills(client, tk) if f.get("trade_id")}
    fee_booked: set[str] = set()  # trade_ids whose fee we've applied — fee is REST-authoritative
    inv = 0.0  # signed YES contracts (FRACTIONAL), from OUR fills; immune to collateral noise
    cash = 0.0  # realized cash: -price on a buy(bid), +price on a sell(ask)
    n_fills = 0
    mid = 0.0
    mids: list[tuple[float, float]] = []  # (wall_ts, mid) — for per-fill markouts
    pnl_hist: list[tuple[float, float]] = []  # (wall_ts, mark P&L) — for the abandon $/min gate
    fill_log: list[tuple[float, str, float, float]] = []  # (fill_ts, side, price, count)
    max_abs_inv = 0.0
    pnl_min = 0.0
    pnl_max = 0.0
    spread_sum = 0.0
    n_polls = 0
    empty_polls = 0  # consecutive polls with no two-sided book (market dying)
    start = time.time()
    last_fill = time.time()  # for the illiquid-exit timeout
    last_scan = time.time()  # for the periodic "is a hotter market available?" scan
    pnl_last_scan = 0.0  # P&L at the previous scan -> our $/min earning rate here
    last_rest_fills = 0.0  # wall ts of the last REST /portfolio/fills reconcile (WS-fills cadence)

    def ingest_fills(force_rest: bool = False) -> None:
        """Account for new fills. With a WS feed, drain the `fill` channel each call for
        low-latency INVENTORY and reconcile against REST every FILL_RECONCILE_S (or when forced —
        on exit); without a feed, poll REST every call (unchanged). trade_id (`seen`) dedups
        inventory across both sources; the FEE is applied once and only from the authoritative
        REST record (a WS fill may omit fee_cost). A WS gap only costs latency, never $."""
        nonlocal inv, cash, n_fills, last_fill, last_rest_fills
        batch: list[tuple[dict[str, Any], bool]] = []  # (fill, is_rest)
        if feed is not None:
            # WS push: low-latency INVENTORY, but the `fill` payload's fee is unverified — so we
            # book inventory now and defer the fee to the authoritative REST record below.
            batch.extend((f, False) for f in feed.drain_fills())
        now = time.time()
        if feed is None or force_rest or now - last_rest_fills >= FILL_RECONCILE_S:
            batch.extend((f, True) for f in fetch_fills(client, tk))  # REST — authoritative (fee)
            last_rest_fills = now
        for f, is_rest in batch:
            tid = f.get("trade_id")
            if not tid:
                continue
            fee = float(f.get("fee_cost") or 0.0)
            if tid in seen:
                # inventory already booked; apply the fee ONCE, only from the REST record (a WS
                # fill may omit fee_cost, so trusting it would drop the taker flatten fee).
                if is_rest and fee and tid not in fee_booked:
                    cash -= fee
                    fee_booked.add(tid)
                continue
            seen.add(tid)
            side = our_orders.get(str(f.get("order_id")))
            if side is None:
                if debug:
                    print(f"  [debug] unattributed fill: {f}")
                continue
            if debug and n_fills == 0:
                print(f"  [debug] first attributed fill: {f}")
            px, cnt = _fill_price(f), _fill_count(f)
            if cnt == 0:
                continue
            if side == "bid":  # our bid filled -> we bought yes
                inv += cnt
                cash -= px * cnt
            else:  # our ask filled -> we sold yes
                inv -= cnt
                cash += px * cnt
            # Fee is REST-authoritative: apply it now for a REST (or non-WS) fill; for a WS-first
            # fill defer it to the REST reconcile (maker fee is 0; only the taker flatten has one,
            # and exit always forces a REST true-up that catches it).
            if is_rest or feed is None:
                cash -= fee
                fee_booked.add(tid)
            fill_log.append((_fill_ts(f), side, px, cnt))
            n_fills += 1
            last_fill = time.time()

    halted = False
    dead_book = False  # set if we exit because the market resolved (book went empty)
    extreme = False  # set if the mid drifts to <10c/>90c (longshot/lock) -> leave + flatten
    illiquid = False  # set if no fills for NO_FILL_TIMEOUT_S (dud market) -> roll on
    thin = False  # set if realized fills/min < THIN_FPM_FLOOR after warmup -> abandon + roll
    switching = False  # set if we leave (while flat) for a much more active market
    switch_target: str | None = None  # the hotter market to switch into
    try:
        while time.time() < end and not halted:
            t0 = time.time()
            try:
                ingest_fills()  # account for fills BEFORE cancelling this poll's orders
                cancel_all(client, tk)
                # Book source: WS local book (real-time, --ws) or a REST snapshot. `feed.top`
                # is a drop-in for best_bid_ask — same (bid, ask) dollars + one-sided->None.
                ba = (
                    feed.top(tk)
                    if feed is not None
                    else best_bid_ask(client.get_market_orderbook(tk))
                )
                if ba is None:  # one-sided/empty book = market dying (e.g. near resolution)
                    empty_polls += 1
                    if empty_polls >= DEAD_BOOK_POLLS:
                        print(
                            f"  book one-sided/empty for {empty_polls} polls — market likely "
                            "resolved; ending session."
                        )
                        dead_book = True
                        break
                    if empty_polls % 3 == 0:
                        print(f"  waiting — no two-sided book ({empty_polls}) ...")
                    time.sleep(poll)
                    continue
                empty_polls = 0
                bid, ask = ba
                mid = (bid + ask) / 2.0
                mids.append((time.time(), mid))
                spread_sum += ask - bid
                n_polls += 1
                max_abs_inv = max(max_abs_inv, abs(inv))
                pnl = cash + inv * mid  # realized + open inventory marked to mid
                pnl_min, pnl_max = min(pnl_min, pnl), max(pnl_max, pnl)
                pnl_hist.append((time.time(), pnl))
                print(f"  inv {inv:+.2f}  mid {mid:.2f}  fills {n_fills}  P&L ${pnl:+.2f}")
                if pnl <= -DAILY_LOSS_LIMIT:  # KILL SWITCH (finally cancels + flattens)
                    print("  KILL SWITCH — stop; flatten on exit.")
                    halted = True
                    break
                if mid < MIN_MID or mid > MAX_MID:  # longshot/lock -> leave WHILE still liquid
                    print(
                        f"  mid {mid:.2f} outside [{MIN_MID:.2f},{MAX_MID:.2f}] — "
                        "leaving (longshot/lock; flatten on exit)."
                    )
                    extreme = True
                    break
                if (
                    time.time() - last_fill > NO_FILL_TIMEOUT_S
                    and time.time() - start > NO_FILL_TIMEOUT_S
                ):
                    print(
                        f"  no fills for {NO_FILL_TIMEOUT_S // 60} min — leaving illiquid market."
                    )
                    illiquid = True
                    break
                # ABANDON THIN-AND-NOT-PAYING: low flow alone isn't enough to leave — a thin
                # market still earning our target (>=GOOD_RATE $/min, the same bar the switch
                # uses to stay sticky) is worth keeping. Abandon only when BOTH hold: fills/min
                # below the floor AND the mark-P&L rate below target. Fires while HOLDING too —
                # the gap the no-fill timeout (zero fills) and flat-only switch both miss.
                # flatten+retire+roll is handled by the finally block + live().
                elapsed = time.time() - start
                if elapsed >= THIN_WARMUP_S:
                    win_s = min(elapsed, THIN_WINDOW_S)
                    cutoff = time.time() - win_s
                    recent = sum(1 for fts, _, _, _ in fill_log if fts >= cutoff)
                    fpm = recent / (win_s / 60.0)
                    pnl_then = next((p for t, p in pnl_hist if t >= cutoff), pnl)
                    pnl_rate = (pnl - pnl_then) / (win_s / 60.0)  # $/min over the same window
                    if fpm < THIN_FPM_FLOOR and pnl_rate < GOOD_RATE:
                        print(
                            f"  flow {fpm:.1f}/min < {THIN_FPM_FLOOR:.0f} and earning "
                            f"${pnl_rate:+.3f}/min < ${GOOD_RATE:.2f} target — thin and not "
                            "paying; leaving (flatten + roll)."
                        )
                        thin = True
                        break
                # CHASE FLOW: while FLAT (free to leave) and past the min dwell, scan for a
                # busier market. The bar is performance-aware: sticky if this market is
                # paying us, eager if we're flat/bleeding (see the constants above).
                if (
                    allow_switch
                    and abs(inv) < FLAT_EPS
                    and time.time() - start >= MIN_DWELL_S
                    and time.time() - last_scan >= SWITCH_SCAN_S
                ):
                    rate = (pnl - pnl_last_scan) / max((time.time() - last_scan) / 60.0, 1e-9)
                    factor = STICKY_FACTOR if rate >= GOOD_RATE else EAGER_FACTOR
                    pnl_last_scan, last_scan = pnl, time.time()
                    cand = better_market(client, tk, retired | {tk}, factor, prefixes)
                    if cand:
                        print(
                            f"  flat, earning ${rate:+.3f}/min here -> need {factor:g}x; "
                            f"{cand} qualifies — switching to chase flow."
                        )
                        switch_target = cand
                        switching = True
                        break
                # INVENTORY SKEW: push the accumulating side off the touch in
                # proportion to inventory so it mean-reverts to flat (long -> lower
                # the bid so we stop buying; short -> raise the ask). The reducing
                # side stays at the touch to keep capturing the spread back to flat.
                skew = SKEW_PER_CONTRACT * inv  # >0 when long, <0 when short
                bid_px = round(max(0.01, min(0.99, bid - max(0.0, skew))), 2)
                ask_px = round(max(0.01, min(0.99, ask - min(0.0, skew))), 2)
                quotes = []  # inventory-capped, skewed, post-only
                if inv < MAX_POSITION:
                    quotes.append(("bid", bid_px))
                if inv > -MAX_POSITION:
                    quotes.append(("ask", ask_px))
                for s_side, s_px in quotes:
                    try:
                        oid = _order_id(
                            place_order(client, tk, s_side, s_px, quote_size, post_only=True)
                        )
                        if oid:
                            our_orders[oid] = s_side
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code != 400:  # 400 = would-cross, skip quietly
                            raise
            except Exception as exc:
                print(f"  loop error: {str(exc)[:120]}")
            time.sleep(max(0.0, poll - (time.time() - t0)))
    finally:
        cancel_all(client, tk)
        ingest_fills(force_rest=True)  # authoritative true-up before we decide what to flatten
        # AUTO-FLATTEN: close residual inventory with an AGGRESSIVE marketable IOC —
        # priced to cross every available level (sell at 0.01 / buy at 0.99) so it
        # fills against whatever liquidity exists, not just the touch. Retry once in
        # case the book refreshes. If the book is truly dead (nothing on the needed
        # side) it can't fill — then the small residual rides to resolution (bounded).
        # Skip flattening if the market resolved (dead book) — there's nothing to
        # trade against and the residual settles automatically at the outcome.
        if not dead_book:
            for attempt in range(2):
                if abs(inv) < FLAT_EPS:
                    break
                s, px = ("ask", 0.01) if inv > 0 else ("bid", 0.99)
                qty = round(abs(inv), 2)
                try:
                    oid = _order_id(place_order(client, tk, s, px, qty, post_only=False, ioc=True))
                    if oid:
                        our_orders[oid] = s
                    print(
                        f"  auto-flatten try {attempt + 1}: IOC {s} {qty:.2f} to close {inv:+.2f}"
                    )
                    time.sleep(0.7)
                    ingest_fills(force_rest=True)
                except Exception as exc:
                    print(f"  auto-flatten error: {str(exc)[:80]}")
            cancel_all(client, tk)
        pnl = cash + inv * mid
        print(
            f"\nDONE. fills {n_fills}.  realized cash ${cash:+.2f}  +  open inv {inv:+.2f} @ "
            f"{mid:.2f}  =  P&L ${pnl:+.2f}"
        )
        if abs(inv) >= FLAT_EPS:
            if dead_book:
                print(
                    f"NOTE: holding {inv:+.2f} on a RESOLVED market — it SETTLES automatically "
                    "at the outcome; no action needed."
                )
            else:
                print(
                    f"NOTE: still holding {inv:+.2f} (flatten incomplete) — close manually in app."
                )

        # --- per-fill markout (the edge signal) + session/fill logging -------
        def _mid_at(ts: float) -> float | None:
            prev: float | None = None
            for t, m in mids:
                if t >= ts:
                    return m
                prev = m
            return prev

        fill_hdr = [
            "ts_utc", "market", "fill_ts", "side", "price", "mid0", "mid_h", "markout_c", "count"
        ]
        marks: list[float] = []
        now_iso = datetime.now(UTC).isoformat(timespec="seconds")
        for fts, fside, fpx, fcnt in fill_log:
            m0, mh = _mid_at(fts), _mid_at(fts + MARKOUT_HORIZON_S)
            mk = None
            if m0 is not None and mh is not None:
                mk = (mh - m0) if fside == "bid" else (m0 - mh)  # >0 = no adverse selection
                marks.append(mk)
            _append_csv(
                FILL_LOG,
                fill_hdr,
                [
                    now_iso,
                    tk,
                    f"{fts:.0f}",
                    fside,
                    f"{fpx:.4f}",
                    "" if m0 is None else f"{m0:.4f}",
                    "" if mh is None else f"{mh:.4f}",
                    "" if mk is None else f"{mk * 100:.3f}",
                    f"{fcnt:.2f}",
                ],
            )
        mean_markout_c = (sum(marks) / len(marks) * 100.0) if marks else float("nan")
        gross, fees = account_pnl(client, tk)
        dur = (mids[-1][0] - mids[0][0]) / 60.0 if len(mids) > 1 else 0.0
        _append_csv(
            SESSION_LOG,
            [
                "ts_utc",
                "market",
                "minutes",
                "n_fills",
                "fills_per_min",
                "net_cash",
                "kalshi_gross",
                "fees",
                "max_abs_inv",
                "pnl_min",
                "pnl_max",
                "avg_spread_c",
                "mean_markout_c",
                "config_version",
                "quote_size",
            ],
            [
                now_iso,
                tk,
                f"{dur:.1f}",
                n_fills,
                f"{n_fills / dur:.1f}" if dur else "0",
                f"{cash:.4f}",
                f"{gross:.4f}",
                f"{fees:.4f}",
                f"{max_abs_inv:.2f}",
                f"{pnl_min:.4f}",
                f"{pnl_max:.4f}",
                f"{spread_sum / n_polls * 100.0 if n_polls else 0.0:.2f}",
                f"{mean_markout_c:.3f}",
                config_tag,
                quote_size,
            ],
        )
        print(
            f"logged -> {SESSION_LOG}: mean markout {mean_markout_c:+.2f}c, max|inv| "
            f"{max_abs_inv:.2f}, swing ${pnl_min:.2f}..${pnl_max:.2f}, realized ${gross:+.2f}"
        )
        if debug:
            try:
                raw = client.get("/portfolio/positions", params={"ticker": tk})
                print(f"  [debug] raw positions response: {raw}")
            except Exception:
                pass
    reason = (
        "kill"
        if halted
        else "dead"
        if dead_book
        else "extreme"
        if extreme
        else "thin"
        if thin
        else "illiquid"
        if illiquid
        else "switch"
        if switching
        else "time"
    )
    return reason, cash + inv * mid, switch_target


def live(
    client: KalshiClient,
    ticker: str | None,
    minutes: float,
    poll: float,
    debug: bool = False,
    switch: bool = True,
    ab: bool = False,
    prefixes: tuple[str, ...] | None = None,
    use_ws: bool = False,
) -> int:
    """Wall-clock session: quote a market, ROLL to a fresh one when it resolves, and
    (if `switch`) jump to a much more active market mid-session whenever we're flat —
    chasing flow toward the edge. A pinned --ticker is run once (no roll, no switch).
    Per-market kill switch (-$DAILY_LOSS_LIMIT) plus a session-cumulative stop.

    `ab` = the size A/B test: randomly quote 1x or 2x PER MARKET (50/50), tagged
    config '<version>-ab' with the size logged per session, so 1x vs 2x can be
    compared on the SAME days/markets — removing the regime confound that makes the
    across-days 1x-vs-2x comparison uninterpretable. The position cap isn't scaled per
    arm because it never binds (inventory sits ~2-3 of 20)."""
    _need_auth(client)
    allow_switch = switch and ticker is None  # pinned market is never switched away from
    end = time.time() + minutes * 60
    sw = (
        f", switches while flat to {EAGER_FACTOR:g}-{STICKY_FACTOR:g}x-busier markets "
        "(eager if flat/bleeding, sticky if earning)"
        if allow_switch
        else ""
    )
    universe = ",".join(prefixes) if prefixes else "all benign sports"
    book_src = (
        "WS local book + `fill` channel (REST reconcile)" if use_ws else "REST orderbook snapshots"
    )
    print(
        f"LIVE session: cap +/-{MAX_POSITION}, kill -${DAILY_LOSS_LIMIT:.0f}/mkt + session, "
        f"{minutes:.0f} min wall-clock — rolls on resolve{sw}. Universe: {universe}. "
        f"Book: {book_src}."
    )
    # WS feeds the per-poll book AND our fills (drained each poll, reconciled vs REST every
    # FILL_RECONCILE_S); cancels + order placement stay REST (Kalshi has no WS order entry).
    # Selection (pick_smooth_ticker) also stays REST — one-shot per candidate, not the hot loop.
    feed = WsBookFeed(client, channels=("orderbook_delta", "fill")) if use_ws else None
    retired: set[str] = set()
    pending: str | None = None  # a switch target to go to next (skips re-selection)
    session_pnl = 0.0
    n_markets = 0
    try:
        while time.time() < end:
            tk = ticker or pending or pick_smooth_ticker(client, exclude=retired, prefixes=prefixes)
            pending = None
            if not tk:
                print("  no fresh active market right now; waiting 15s ...")
                time.sleep(15)
                continue
            n_markets += 1
            size = random.choice([1, 2]) if ab else QUOTE_SIZE
            # Tag the session by ruleset so analysis can separate runs. '-ws' marks a
            # WS-fed book (so WS-vs-REST capture/markout is comparable, not blended).
            tag = CONFIG_VERSION + ("-ab" if ab else "") + ("-ws" if use_ws else "")
            ab_note = f" [AB {size}x]" if ab else ""
            print(
                f"\n=== market {n_markets}: {tk}  "
                f"({(end - time.time()) / 60:.0f} min left){ab_note} ==="
            )
            if feed is not None:  # subscribe this market's book on the WS, wait for first snapshot
                feed.subscribe(tk)
                ready = feed.wait_for_book(tk, timeout=5.0)
                print(f"  [ws] book {'live' if ready else 'not ready (loop will wait)'} for {tk}")
            reason, pnl, nxt = _run_market(
                client, tk, end, poll, debug, retired, allow_switch, size, tag, prefixes, feed=feed
            )
            session_pnl += pnl
            if reason == "switch":
                pending = nxt  # go straight to the hotter market; don't retire this one
            else:
                retired.add(tk)  # resolved/dud/left markets won't be re-picked this session
            print(f"  [{tk}] ended ({reason}); session P&L so far ${session_pnl:+.2f}")
            if reason == "kill" or session_pnl <= -DAILY_LOSS_LIMIT:
                print("  SESSION KILL — stopping.")
                break
            if ticker:  # a pinned market can't be rolled
                break
    finally:
        if feed is not None:
            feed.close()
    print(f"\nSESSION DONE: {n_markets} market(s), total mark P&L ${session_pnl:+.2f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auth-check", action="store_true")
    ap.add_argument("--test-order", action="store_true")
    ap.add_argument("--cancel-all", action="store_true", help="panic: cancel all resting orders")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--i-understand-live", action="store_true", help="required with --live")
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--minutes", type=float, default=15.0)
    ap.add_argument("--poll", type=float, default=POLL_SECONDS)
    ap.add_argument("--debug", action="store_true", help="dump raw fill records (verify shape)")
    ap.add_argument(
        "--no-switch",
        action="store_true",
        help="disable mid-session switching to busier markets (stay until each resolves)",
    )
    ap.add_argument(
        "--ab",
        action="store_true",
        help="A/B size test: randomize 1x/2x quote size per market (logs quote_size, tag '-ab')",
    )
    ap.add_argument(
        "--prefix",
        default=None,
        help="restrict to ticker prefix(es), comma-separated. Use KXWC for World-Cup-only "
        "(the only consistently profitable cell; basketball/baseball are toxic). e.g. KXWC",
    )
    ap.add_argument(
        "--ws",
        action="store_true",
        help="source market data over WebSocket: the per-poll order book AND our fills "
        "(REST-reconciled). Order placement/cancel stay REST (no WS order entry). Single-market.",
    )
    args = ap.parse_args()

    load_dotenv()  # pick up KALSHI_API_BASE/KEY_ID + the inline key from .env
    try:
        client = KalshiClient(pace_seconds=0.1)
    except Exception as exc:
        print(f"Failed to load credentials: {str(exc)[:200]}", file=sys.stderr)
        print(
            "The inline PEM in .env must be a valid RSA private key — double-quoted "
            "multiline, or one line with \\n escapes.",
            file=sys.stderr,
        )
        return 2
    try:
        if args.auth_check:
            return auth_check(client)
        if args.test_order:
            return test_order(client, args.ticker)
        if args.cancel_all:
            _need_auth(client)
            print(f"cancelled {cancel_all(client, args.ticker)} resting order(s).")
            return 0
        if args.live:
            if not args.i_understand_live:
                print("Refusing --live without --i-understand-live (real money).", file=sys.stderr)
                return 2
            prefixes = tuple(p.strip() for p in args.prefix.split(",")) if args.prefix else None
            return live(
                client, args.ticker, args.minutes, args.poll, args.debug,
                switch=not args.no_switch, ab=args.ab, prefixes=prefixes, use_ws=args.ws,
            )
        print("Pick a mode: --auth-check | --test-order | --live --i-understand-live")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
