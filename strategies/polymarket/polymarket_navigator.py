"""Polymarket feasibility navigator — READ-ONLY probe: can our mean-reverting total/spread
MM strategy run on Polymarket too, and can we accumulate data there?

Polymarket market data is FULLY PUBLIC (no auth/KYC to READ): Gamma API (markets/events)
+ CLOB API (order book / prices / spread). This probe answers three questions:
  1. Are the public endpoints reachable from here? (read-only, no account)
  2. STRUCTURE: of the high-volume markets, how many are MEAN-REVERTING (total / spread /
     over-under, the shape our edge needs) vs DIRECTIONAL (winner / event / futures, the
     shape that failed for us on tennis & crypto)? And how many sit in our band
     (mid 0.05-0.92) with a makeable spread + real 24h volume?
  3. Can we read a live order book per outcome token (the logger prerequisite)?

It places NO orders and needs NO account. (Trading on Polymarket US separately requires
KYC via their iOS app + Ed25519 keys through the CFTC-regulated entity — out of scope here.)

Usage:
    uv run python -m strategies.polymarket.polymarket_navigator
    uv run python -m strategies.polymarket.polymarket_navigator --limit 1000
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
SNAPSHOT_LOG = "data/polymarket_snapshots.csv"  # one row per run -> compare across time-of-day
# Question keywords that mark a genuine MEAN-REVERTING line market (a number to oscillate
# around) vs directional winner/event markets that trend to 0/1 and pick makers off.
# Deliberately tight — "X by <date>" political markets falsely match loose words like "by",
# so we keep only unambiguous over-under / spread / handicap markers.
MEANREV_KW = ("o/u", "over/under", "over under", "over/ under", " spread ",
              "handicap", "to cover", "margin of")
MIN_MID, MAX_MID = 0.05, 0.92  # same band as the Kalshi maker
MIN_SPREAD, MAX_SPREAD = 0.01, 0.15  # makeable: enough to capture, not an empty book


def _jget(v: Any, default: Any) -> Any:
    """Gamma encodes list fields (outcomes, prices, tokenIds) as JSON strings."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return default
    return v if v is not None else default


def is_meanrev(question: str, tags: list[str]) -> bool:
    hay = (question + " " + " ".join(tags)).lower()
    return any(k in hay for k in MEANREV_KW)


def fetch_markets(client: httpx.Client, limit: int) -> list[dict[str, Any]]:
    """Top active markets by 24h volume (Gamma API, public)."""
    out: list[dict[str, Any]] = []
    offset = 0
    while len(out) < limit:
        r = client.get(
            f"{GAMMA}/markets",
            params={"active": "true", "closed": "false", "order": "volume24hr",
                    "ascending": "false", "limit": min(500, limit - len(out)), "offset": offset},
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        offset += len(batch)
    return out


def probe_book(client: httpx.Client, token_id: str) -> str:
    """Read one outcome token's order book to confirm we can get depth."""
    r = client.get(f"{CLOB}/book", params={"token_id": token_id})
    r.raise_for_status()
    b = r.json()
    bids, asks = b.get("bids", []), b.get("asks", [])
    if not bids or not asks:
        return "book returned but one-sided/empty"
    bb = max(float(o["price"]) for o in bids)
    ba = min(float(o["price"]) for o in asks)
    return f"OK — {len(bids)} bid levels / {len(asks)} ask levels, top {bb:.3f}/{ba:.3f}"


USDC = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"  # Polygon USDC.e (reward payout asset)


def fetch_reward_markets(client: httpx.Client) -> list[dict[str, Any]]:
    """All reward-eligible markets (CLOB /sampling-markets, paginated). Each carries its
    `rewards` config: {rates:[{asset_address, rewards_daily_rate}], min_size, max_spread}."""
    out: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(60):  # bound pagination
        r = client.get(f"{CLOB}/sampling-markets", params={"next_cursor": cursor} if cursor else {})
        r.raise_for_status()
        d = r.json()
        out.extend(d.get("data", []))
        cursor = d.get("next_cursor", "")
        if not cursor or cursor == "LTE=":
            break
    return out


def model_rewards(client: httpx.Client, deploy: float) -> list[tuple[Any, ...]]:
    """For each reward-eligible MEAN-REVERTING in-band market, estimate the daily reward
    yield of deploying `deploy` USDC two-sided near the mid. First-order model:
    our reward share ~ q / (q + C) where q = our per-side size and C = competing qualifying
    depth (others' resting size within max_spread of mid). Ignores the quadratic
    closeness-to-mid weighting (so treat the APR as an order-of-magnitude, not a quote)."""
    rows: list[tuple[Any, ...]] = []
    eligible = []
    for m in fetch_reward_markets(client):
        rw = m.get("rewards") or {}
        r_usd = sum(float(x.get("rewards_daily_rate") or 0)
                    for x in (rw.get("rates") or [])
                    if str(x.get("asset_address", "")).lower() == USDC)
        if r_usd < 1.0 or not is_meanrev(str(m.get("question", "")), []):
            continue  # skip dust pools + directional markets
        if m.get("tokens"):
            eligible.append((r_usd, m))
    eligible.sort(reverse=True, key=lambda x: x[0])
    for r_usd, m in eligible[:40]:
        tok = str(m["tokens"][0]["token_id"])
        try:
            b = client.get(f"{CLOB}/book", params={"token_id": tok}).json()
        except Exception:
            continue
        bids, asks = b.get("bids", []), b.get("asks", [])
        if not bids or not asks:
            continue
        bb = max(float(o["price"]) for o in bids)
        ba = min(float(o["price"]) for o in asks)
        mid, spread = (bb + ba) / 2, ba - bb
        # drop dead/pre-game books: a wide spread means there's no live market to make,
        # so the "uncontested pool" is a mirage (you'd be alone but there's no flow).
        if not (MIN_MID <= mid <= MAX_MID) or spread > 0.10:
            continue
        band = float((m.get("rewards") or {}).get("max_spread") or 3) / 100.0
        c_bid = sum(float(o["size"]) for o in bids if 0 <= mid - float(o["price"]) <= band)
        c_ask = sum(float(o["size"]) for o in asks if 0 <= float(o["price"]) - mid <= band)
        c_side = (c_bid + c_ask) / 2  # ~ per-side competing depth (shares ~ USDC two-sided)
        if c_side < 50:  # too thin to be a genuinely contested live reward market
            continue
        daily = r_usd * deploy / (deploy + c_side) if (deploy + c_side) else 0.0
        apr = 365 * daily / deploy if deploy else 0.0
        rows.append((apr, daily, r_usd, c_side, mid, spread * 100, band * 100, str(m["question"])))
    rows.sort(reverse=True)
    return rows


def run_rewards(client: httpx.Client, deploy: float) -> int:
    print("=" * 74)
    print(f"POLYMARKET REWARD-YIELD MODEL  (deploy ${deploy:,.0f} two-sided near mid)")
    print("=" * 74)
    rows = model_rewards(client, deploy)
    if not rows:
        print("No reward-eligible mean-reverting in-band markets found right now.")
        return 0
    hdr = f"{'APR%':>7}{'$/day':>8}{'pool/d':>8}{'compete':>9}{'mid':>6}{'spr':>6}{'maxsp':>6}"
    print(hdr + "  market")
    print("-" * 74)
    for apr, daily, r_usd, c_side, mid, spr, maxsp, q in rows[:15]:
        print(f"{apr * 100:>6.0f}%{daily:>8.2f}{r_usd:>8.0f}{c_side:>9,.0f}"
              f"{mid:>6.2f}{spr:>5.1f}c{maxsp:>5.1f}c  {q[:30]}")
    best = rows[0]
    print(f"\nBest modeled: {best[0] * 100:.0f}% APR (${best[1]:.2f}/day on ${deploy:,.0f}) — "
          f"BEFORE goal-jump adverse selection + inventory + crypto/KYC friction.")
    print("Model is first-order (ignores quadratic closeness weighting + min-side mechanics);")
    print("read it as an order of magnitude, and net of pick-off the real number is lower.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--rewards", action="store_true", help="model liquidity-reward yield instead")
    ap.add_argument("--deploy", type=float, default=500.0, help="USDC to model deploying")
    args = ap.parse_args()
    client = httpx.Client(timeout=20.0, headers={"User-Agent": "de-portfolio-research/1.0"})

    if args.rewards:
        try:
            return run_rewards(client, args.deploy)
        finally:
            client.close()

    now = datetime.now(UTC)
    et_hr = (now.hour - 4) % 24  # rough ET hour for the time-of-day note
    print("=" * 74)
    print(f"POLYMARKET FEASIBILITY NAVIGATOR (read-only)  {now:%Y-%m-%d %H:%M}Z (~{et_hr:02d}h ET)")
    print("=" * 74)
    try:
        markets = fetch_markets(client, args.limit)
    except Exception as exc:
        print(f"FAIL: could not reach Gamma API: {str(exc)[:160]}")
        return 1
    print(f"1) Gamma API reachable: pulled {len(markets)} active markets by 24h volume.\n")

    meanrev = directional = 0
    candidates: list[tuple[float, str, float, float]] = []  # (vol, q, mid, spread)
    mr_band: list[tuple[float, float]] = []  # (spread, vol) of ALL mean-rev in-band markets
    a_token = None
    for m in markets:
        q = str(m.get("question", ""))
        tags = [str(t) for t in _jget(m.get("tags"), [])]
        prices = [float(x) for x in _jget(m.get("outcomePrices"), []) if x not in ("", None)]
        bb, ba = m.get("bestBid"), m.get("bestAsk")
        mid = ((float(bb) + float(ba)) / 2) if bb and ba else (prices[0] if prices else 0.0)
        spread = float(m.get("spread") or (float(ba) - float(bb) if bb and ba else 0.0))
        vol = float(m.get("volume24hr") or 0.0)
        mr = is_meanrev(q, tags)
        meanrev += mr
        directional += not mr
        if a_token is None:
            toks = _jget(m.get("clobTokenIds"), [])
            if toks:
                a_token = str(toks[0])
        if mr and MIN_MID <= mid <= MAX_MID:
            mr_band.append((spread, vol))
            if MIN_SPREAD <= spread <= MAX_SPREAD and vol >= 5000:
                candidates.append((vol, q, mid, spread))

    print(f"2) STRUCTURE of {len(markets)} markets:")
    print(f"   mean-reverting (total/spread/o-u): {meanrev:>4}   "
          f"directional (winner/event): {directional:>4}")

    # The maker-edge check: among mean-reverting in-band markets, is there a WIDE-spread AND
    # liquid pocket, or are they all stuck at the 1c tick? (wide + no volume = "wide != rich")
    bands = [(0.0, 0.02, "<2c"), (0.02, 0.05, "2-5c"), (0.05, 0.15, "5-15c"), (0.15, 1.0, ">15c")]
    print(f"\n   SPREAD x VOLUME of the {len(mr_band)} mean-reverting in-band markets:")
    counts: list[int] = []
    for lo, hi, lbl in bands:
        sub = [(s, v) for s, v in mr_band if lo <= s < hi]
        counts.append(len(sub))
        liquid = sum(1 for s, v in sub if v >= 5000)
        med = sorted(v for _, v in sub)[len(sub) // 2] if sub else 0.0
        print(f"     {lbl:<5}: {len(sub):>3} mkts ({liquid} >=$5k)  median vol ${med:,.0f}")

    print(f"\n   MAKEABLE (band 0.05-0.92, spread {MIN_SPREAD}-{MAX_SPREAD}, >=$5k/24h): "
          f"{len(candidates)} markets")
    for vol, q, mid, spread in sorted(candidates, reverse=True)[:8]:
        print(f"     ${vol:>10,.0f}/24h  mid {mid:.2f}  spread {spread:.3f}  {q[:46]}")

    p = Path(SNAPSHOT_LOG)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["ts_utc", "et_hr", "n_markets", "n_meanrev", "n_inband",
                        "n_makeable", "sp_lt2c", "sp_2_5c", "sp_5_15c", "sp_gt15c"])
        w.writerow([now.isoformat(timespec="minutes"), et_hr, len(markets), meanrev,
                    len(mr_band), len(candidates), *counts])
    print(f"\n   (snapshot appended to {SNAPSHOT_LOG} — re-run at midday/evening to compare)")

    print()
    if a_token:
        try:
            print(f"3) Order-book read (token {a_token[:14]}...): {probe_book(client, a_token)}")
        except Exception as exc:
            print(f"3) Order-book read FAILED: {str(exc)[:120]}")
    client.close()

    print("\nREAD: API + data are reachable read-only. FEASIBILITY hinges on the structure")
    print("count above — our edge is live SPORTS TOTALS/SPREADS; if those are ~0 here and the")
    print("volume is winner/event/futures markets, the strategy does NOT transfer (same reason")
    print("tennis/crypto failed: directional markets trend, don't round-trip).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
