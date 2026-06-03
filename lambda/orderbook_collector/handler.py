"""AWS Lambda: 24/7 KXBTC15M order-book collector for the decision-minute study.

Graduates the local launchd collector (ingestion/kalshi_orderbook.py) to a
reliable serverless job. EventBridge fires it at the decision minutes we want to
study — W+1 (control), W+12 (the candidate cluster), W+14 (near expiry) of every
15-min window — and each invocation snapshots the live executable book + BTC spot
a few times ~20s apart, then writes one JSONL object to S3:

    s3://$S3_BUCKET/raw/orderbook_snapshots/dt=YYYY-MM-DD/<ticker>_wk<k>.jsonl

The repeated snapshots measure how fast Kalshi reprices (the lead-lag); the W+k in
the key/rows lets the reconciliation (ml/live_exec_reconcile.py) test each minute.

DELIBERATELY DEPENDENCY-FREE: only the Python stdlib + boto3 (present in the Lambda
runtime). No httpx / cryptography / pyarrow to bundle, so the deployment zip is just
this file — no native wheels, no cross-compilation. Read-only public Kalshi market
data; no auth, no orders, no money.

Local test (writes a real snapshot to S3 with your default creds):
    uv run python lambda/orderbook_collector/handler.py
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

import boto3

SERIES = "KXBTC15M"
COINBASE_TICKER = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
RAW_PREFIX = "raw/orderbook_snapshots"
DEPTH = 50
SNAPSHOTS = int(os.environ.get("SNAPSHOTS", "3"))
INTERVAL = float(os.environ.get("INTERVAL", "20"))
HTTP_TIMEOUT = 10.0


def _get_json(url: str, params: dict[str, str | int] | None = None, retries: int = 3) -> Any:
    """GET + parse JSON with light retry/backoff on 429/5xx (the public API rate-limits)."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(0.5 * (2**attempt))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(0.5 * (2**attempt))
                continue
            raise
    raise RuntimeError(f"GET failed: {url}") from last


def _api_base() -> str:
    base = os.environ.get("KALSHI_API_BASE", "").rstrip("/")
    if not base:
        raise RuntimeError("KALSHI_API_BASE not set")
    return base


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def active_market(api_base: str) -> dict[str, Any] | None:
    """The open KXBTC15M market whose 15-min window currently contains `now` — the
    freshly-opened decision window (latest open_time among those containing now)."""
    resp = _get_json(f"{api_base}/markets", {"series_ticker": SERIES, "status": "open"})
    markets = resp.get("markets", [])
    if not markets:
        return None
    now = datetime.now(UTC)
    in_window = [
        m for m in markets if _parse_iso(m["open_time"]) <= now < _parse_iso(m["close_time"])
    ]
    pool = in_window or markets
    chosen: dict[str, Any] = max(pool, key=lambda m: _parse_iso(m["open_time"]))
    return chosen


def top_of_book(book: dict[str, Any]) -> dict[str, Any]:
    """Reduce a raw orderbook_fp dict to the touch + total depth. A 'no' bid at price
    p is a 'yes' ask at (1 - p), so best_yes_ask = 1 - best_no_bid."""
    yes = [(float(p), float(q)) for p, q in (book.get("yes_dollars") or [])]
    no = [(float(p), float(q)) for p, q in (book.get("no_dollars") or [])]
    best_yes_bid = max((p for p, _ in yes), default=None)
    best_no_bid = max((p for p, _ in no), default=None)
    best_yes_ask = round(1.0 - best_no_bid, 4) if best_no_bid is not None else None

    mid = spread = None
    if best_yes_bid is not None and best_yes_ask is not None:
        mid = round((best_yes_ask + best_yes_bid) / 2, 4)
        spread = round(best_yes_ask - best_yes_bid, 4)
    return {
        "best_yes_bid": best_yes_bid,
        "best_yes_ask": best_yes_ask,
        "yes_bid_size": dict(yes).get(best_yes_bid) if best_yes_bid is not None else None,
        "yes_ask_size": dict(no).get(best_no_bid) if best_no_bid is not None else None,
        "mid": mid,
        "spread": spread,
        "yes_levels": len(yes),
        "no_levels": len(no),
        "yes_depth": round(sum(q for _, q in yes), 1),
        "no_depth": round(sum(q for _, q in no), 1),
    }


def _btc_spot() -> float | None:
    try:
        return float(_get_json(COINBASE_TICKER)["price"])
    except (KeyError, ValueError, urllib.error.URLError, urllib.error.HTTPError, RuntimeError):
        return None


def _decision_offset_min(window_open: datetime, captured: datetime) -> int:
    """Whole-minute offset W+k of this capture from the window open (for the key/rows)."""
    return max(0, round((captured - window_open).total_seconds() / 60.0))


def handler(event: Any, context: Any) -> dict[str, Any]:  # noqa: ARG001
    api_base = _api_base()
    bucket = os.environ["S3_BUCKET"]
    market = active_market(api_base)
    if market is None:
        return {"ok": True, "captured": 0, "reason": "no open KXBTC15M window"}

    ticker = market["ticker"]
    window_open = _parse_iso(market["open_time"])
    rows: list[dict[str, Any]] = []
    for i in range(SNAPSHOTS):
        now = datetime.now(UTC)
        book = _get_json(f"{api_base}/markets/{ticker}/orderbook", {"depth": DEPTH})
        tob = top_of_book(book.get("orderbook_fp") or book.get("orderbook") or {})
        rows.append(
            {
                "captured_at": now.isoformat(),
                "market_ticker": ticker,
                "window_open_at": market["open_time"],
                "window_close_at": market["close_time"],
                "decision_offset_min": _decision_offset_min(window_open, now),
                "btc_spot": _btc_spot(),
                **tob,
            }
        )
        if i < SNAPSHOTS - 1:
            time.sleep(INTERVAL)

    k = rows[0]["decision_offset_min"]
    day = window_open.date().isoformat()
    key = f"{RAW_PREFIX}/dt={day}/{ticker}_wk{k}.jsonl"
    body = "\n".join(json.dumps(r) for r in rows) + "\n"
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=body.encode())
    return {"ok": True, "captured": len(rows), "ticker": ticker, "wk": k, "s3_key": key}


if __name__ == "__main__":
    # Local smoke test: load .env, take a quick 2x burst, write to S3 with default creds.
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    os.environ.setdefault("SNAPSHOTS", "2")
    os.environ.setdefault("INTERVAL", "2")
    SNAPSHOTS = int(os.environ["SNAPSHOTS"])
    INTERVAL = float(os.environ["INTERVAL"])
    print(json.dumps(handler(None, None), indent=2))
