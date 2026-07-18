"""Offline unit tests for the WS book-feed bridge (ml.lp.ws_book_feed).

The whole point of the bridge is that ``feed.top()`` is a BYTE-FOR-BYTE drop-in for
``ml.lp.lp_pilot.best_bid_ask`` — same dollar units, same ``ask = 1 - best_no`` convention,
and the same one-sided/absent-book -> ``None`` contract. If that equivalence holds, swapping
the maker's per-poll book read for the WS feed cannot change the strategy. These tests pin
it. No network, no thread: we inject a LocalBook and read it synchronously."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any, cast

from ingestion.kalshi import KalshiClient
from ingestion.kalshi_ws import KalshiWS, LocalBook
from ml.lp.lp_live import _fill_count, _fill_price, _fill_ts
from ml.lp.lp_pilot import best_bid_ask
from ml.lp.ws_book_feed import WsBookFeed


def _ws() -> KalshiWS:
    return KalshiWS(cast(KalshiClient, object()))  # _handle/drain never touch the client


def _feed() -> WsBookFeed:
    # The client is only used when the WS connects; top() never connects, so a fake is safe.
    return WsBookFeed(cast(KalshiClient, object()))


def _book(yes: dict[str, str], no: dict[str, str]) -> LocalBook:
    b = LocalBook()
    b.yes = {p: Decimal(s) for p, s in yes.items()}
    b.no = {p: Decimal(s) for p, s in no.items()}
    return b


def test_top_matches_best_bid_ask_two_sided() -> None:
    feed = _feed()
    # multiple levels per side -> top() must pick the BEST (max) on each, like best_bid_ask
    feed._ws.books["KX"] = _book({"0.40": "10", "0.38": "5"}, {"0.55": "8", "0.53": "3"})
    # bid = best yes = 0.40; ask = 1 - best no (0.55) = 0.45
    assert feed.top("KX") == (0.40, 0.45)
    # identical to the REST path on an equivalent orderbook payload
    rest = {
        "yes_dollars": [["0.40", "10"], ["0.38", "5"]],
        "no_dollars": [["0.55", "8"], ["0.53", "3"]],
    }
    assert feed.top("KX") == best_bid_ask(rest)


def test_top_one_sided_is_none() -> None:
    """One-sided WS books return None (not (bid, None)) so the maker treats them as empty
    polls exactly like best_bid_ask does — the contract the swap relies on."""
    feed = _feed()
    feed._ws.books["NO_ASK"] = _book({"0.40": "10"}, {})
    feed._ws.books["NO_BID"] = _book({}, {"0.55": "8"})
    assert feed.top("NO_ASK") is None
    assert feed.top("NO_BID") is None
    assert best_bid_ask({"yes_dollars": [["0.40", "10"]], "no_dollars": []}) is None


def test_top_absent_book_is_none() -> None:
    # never subscribed / no snapshot yet -> None, same as a one-sided REST book
    assert _feed().top("NEVER_SUBSCRIBED") is None


# --- subscription: fill must be account-wide, not market-scoped -------------
class _RecordingWS:
    """Captures the JSON frames subscribe() sends, so we can assert channel scoping."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def test_subscribe_fill_is_account_wide() -> None:
    """The bug: subscribing `fill` with market_tickers filters it to the initial market, so it
    goes silent after the maker rolls. `fill` (account-wide) must be sent with NO market filter;
    `orderbook_delta` (market-scoped) must carry the market list."""
    ws = _ws()
    rec = _RecordingWS()
    ws._ws = rec
    asyncio.run(ws.subscribe(["KXWC-A"], ("orderbook_delta", "fill")))
    params = {ch: m["params"] for m in rec.sent for ch in m["params"]["channels"]}
    assert params["orderbook_delta"]["market_tickers"] == ["KXWC-A"]   # market-scoped
    assert "market_tickers" not in params["fill"]                       # account-wide, no filter


# --- fill channel -----------------------------------------------------------
def test_fill_channel_buffers_and_drains() -> None:
    ws = _ws()
    ws._handle({"type": "fill", "msg": {"trade_id": "t1", "order_id": "o1",
                "market_ticker": "KX", "yes_price_dollars": "0.40", "count_fp": "2.00",
                "fee_cost": "0", "ts_ms": 1700000000000}})
    drained = ws.drain_fills()
    assert len(drained) == 1 and drained[0]["trade_id"] == "t1"
    assert ws.drain_fills() == []  # drain clears the buffer


def test_error_message_is_logged_not_buffered() -> None:
    ws = _ws()
    ws._handle({"type": "error", "msg": {"code": 6, "msg": "bad channel"}})
    assert ws.drain_fills() == [] and ws.trades == []  # errors never look like fills/trades


def test_fill_ts_normalizes_ws_ms_and_rest_seconds() -> None:
    assert _fill_ts({"ts": 1700000000.0}) == 1700000000.0  # REST: epoch seconds
    assert _fill_ts({"ts_ms": 1700000000000}) == 1700000000.0  # WS: ms -> seconds
    assert isinstance(_fill_ts({}), float)  # neither present -> now() fallback


def test_ws_and_rest_fill_parse_identically() -> None:
    """The SAME canonical fill from REST vs the WS `fill` channel must parse to identical
    price/count — that (plus shared trade_id) is what makes feeding from both sources safe:
    the maker's `seen` set dedups by trade_id, so no double-count and no inventory drift."""
    rest = {"trade_id": "t1", "order_id": "o1", "yes_price_dollars": "0.40", "count_fp": "2.00"}
    ws = {"trade_id": "t1", "order_id": "o1", "yes_price_dollars": "0.40", "count_fp": "2.00",
          "ts_ms": 1700000000000, "is_taker": False}
    assert _fill_price(rest) == _fill_price(ws) == 0.40
    assert _fill_count(rest) == _fill_count(ws) == 2.0
