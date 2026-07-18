"""Background WebSocket book feed for the live maker — runs ``ingestion.kalshi_ws.KalshiWS``
in a daemon thread so the SYNCHRONOUS maker loop (``ml.lp.lp_live``) can read a real-time
local order book instead of polling REST once per cycle.

WHY A THREAD (and not a rewrite). The maker is a synchronous poll loop (``time.sleep``);
``KalshiWS`` is asyncio. Rather than rewrite the maker async — that is the multi-market
Phase-5 scaling, a much bigger change — we run the WS client in its own event loop on a
daemon thread and hand the maker a thread-safe top-of-book read. The order book is the
ONLY market data the maker pulls inside the loop; fills, cancels, and order placement stay
on REST (there is no WS path wired for those). So this swaps EXACTLY one call:

    best_bid_ask(client.get_market_orderbook(tk))   ->   feed.top(tk)

``top()`` mirrors ``ml.lp.lp_pilot.best_bid_ask`` exactly — ``(bid, ask)`` in dollars with
``ask = 1 - best_no``, or ``None`` when the book is one-sided/absent — so it is a drop-in
and the strategy logic is byte-for-byte unchanged. The only differences the maker sees are
a fresher book (pushed in real time vs polled) and one fewer REST call per poll.

This is the SINGLE-MARKET swap: it feeds whichever one market the maker is currently
quoting (subscribing each new market on the existing connection as the maker rolls). The
real WS payoff — quoting many markets at once — is the multi-market maker, not this.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from ingestion.kalshi import KalshiClient
from ingestion.kalshi_ws import KalshiWS, LocalBook


def _safe_top(book: LocalBook) -> tuple[float | None, float | None]:
    """``top_of_book()`` iterates the level dicts, which the WS thread mutates in place; a
    concurrent resize raises ``RuntimeError: dictionary changed size during iteration``.
    The window is microscopic, so just retry a few times before giving up."""
    for _ in range(5):
        try:
            return book.top_of_book()
        except RuntimeError:
            continue
    return None, None


class WsBookFeed:
    """Owns a ``KalshiWS`` on a daemon thread and exposes a synchronous, thread-safe
    top-of-book read for the maker. One connection; markets are added as the maker rolls
    (never removed — a few idle subscriptions cost nothing and keep the lifecycle simple)."""

    def __init__(
        self, client: KalshiClient, channels: tuple[str, ...] = ("orderbook_delta",)
    ) -> None:
        self._ws = KalshiWS(client)
        self._channels = channels  # ("orderbook_delta",) book-only, +("fill",) for our executions
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()  # set once the bg event loop exists
        self._subscribed: set[str] = set()

    # --- lifecycle ----------------------------------------------------------
    def start(self, tickers: list[str]) -> None:
        """Spin up the bg event loop + resilient WS recv loop, subscribed to ``tickers``."""
        self._subscribed = set(tickers)
        self._ws.set_subscription(list(tickers), self._channels)
        self._thread = threading.Thread(target=self._run, name="ws-book-feed", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)  # block until the loop is up (so ensure_subscribed works)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_until_complete(self._ws.run_forever())  # exits when close() sets _stop

    def subscribe(self, ticker: str) -> None:
        """Ensure ``ticker``'s book is being maintained — starts the thread on first call,
        adds the market to the live connection on subsequent calls."""
        if self._thread is None:
            self.start([ticker])
        else:
            self.ensure_subscribed(ticker)

    def ensure_subscribed(self, ticker: str) -> None:
        """Add a newly-rolled market on the existing connection (no reconnect). Idempotent;
        no-op if the bg loop isn't up yet (then a reconnect would resubscribe the set)."""
        if self._loop is None or ticker in self._subscribed:
            return
        self._subscribed.add(ticker)
        fut = asyncio.run_coroutine_threadsafe(
            self._ws.update_subscription("orderbook_delta", [ticker], "add_markets"),
            self._loop,
        )
        try:
            fut.result(timeout=5.0)
        except Exception:  # transient WS hiccup; top() just returns None until the book lands
            pass

    def wait_for_book(self, ticker: str, timeout: float = 5.0) -> bool:
        """Block up to ``timeout`` for the first two-sided snapshot of ``ticker``. Returns
        whether a book arrived (the maker can proceed either way — a None book reads as an
        empty poll and the loop waits)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.top(ticker) is not None:
                return True
            time.sleep(0.1)
        return False

    # --- the one call the maker needs ---------------------------------------
    def top(self, ticker: str) -> tuple[float, float] | None:
        """``(bid, ask)`` in dollars, or ``None`` if the book is one-sided/absent — a drop-in
        for ``best_bid_ask(client.get_market_orderbook(ticker))`` (same units + convention +
        one-sided->None contract)."""
        book = self._ws.books.get(ticker)
        if book is None:
            return None
        bid, ask = _safe_top(book)
        if bid is None or ask is None:  # collapse one-sided -> None, like best_bid_ask
            return None
        return bid, ask

    def drain_fills(self) -> list[dict[str, Any]]:
        """Return + clear OUR fills pushed on the `fill` channel since the last call (empty if
        the feed wasn't subscribed to `fill`). The maker dedups these against REST by trade_id,
        so this is a best-effort low-latency source, never the sole source of truth."""
        return self._ws.drain_fills()

    def close(self) -> None:
        """Stop the WS recv loop and join the thread (best-effort, bounded)."""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
