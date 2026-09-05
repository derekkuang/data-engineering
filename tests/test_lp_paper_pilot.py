"""Unit tests for the zero-capital paper-maker (core/maker/lp_paper_pilot.py).

Covers the pieces the multi-market politics upgrade touches — none need the network:
  * pick_benign_tickers: top-N makeable selection (spread filter + EXCLUDE + series_set override),
  * poll_once: fill simulation against the touch + trade-id dedup,
  * markout_rows: the pooled net-question arithmetic across many markets (the whole point of
    --markets: grow the fill sample without mixing mid trajectories).
"""

from __future__ import annotations

import pytest

from core.maker.lp_paper_pilot import (
    Fill,
    Pilot,
    best_bid_ask,
    markout_rows,
    pick_benign_ticker,
    pick_benign_tickers,
    poll_once,
)


def _book(yes_bid: float, no_bid: float) -> dict:
    """A minimal orderbook: best yes bid = yes_bid, best yes ask = 1 - no_bid."""
    return {"yes_dollars": [[yes_bid, 100]], "no_dollars": [[no_bid, 100]]}


class FakeClient:
    def __init__(self, books, global_trades=None, per_ticker_trades=None, series=None):
        self.books = books
        self.global_trades = global_trades or []
        self.per_ticker_trades = per_ticker_trades or {}
        self.series = series or []

    def get(self, path, params=None):
        params = params or {}
        if path == "/markets/trades":
            if "ticker" in params:
                return {"trades": self.per_ticker_trades.get(params["ticker"], [])}
            return {"trades": self.global_trades}
        return {}

    def get_market_orderbook(self, ticker):
        return self.books.get(ticker, {})

    def list_series(self):
        return self.series

    def close(self):
        pass


def test_best_bid_ask():
    assert best_bid_ask(_book(0.60, 0.37)) == (0.60, 0.63)
    assert best_bid_ask({"yes_dollars": [], "no_dollars": []}) is None


def test_pick_benign_tickers_topn_and_spread_filter():
    # A busiest + makeable (3c), B next + makeable (2c), C busy but broken (40c spread -> excluded).
    books = {
        "KXMLBGAME-A": _book(0.60, 0.37),  # spread 0.03 -> makeable
        "KXMLBGAME-B": _book(0.70, 0.28),  # spread 0.02 -> makeable
        "KXMLBGAME-C": _book(0.50, 0.10),  # spread 0.40 -> too wide
    }
    trades = (
        [{"ticker": "KXMLBGAME-A"}] * 5
        + [{"ticker": "KXMLBGAME-B"}] * 3
        + [{"ticker": "KXMLBGAME-C"}] * 4
        + [{"ticker": "KXBTC-15M-X"}] * 9  # EXCLUDEd fast series -> never counted
    )
    client = FakeClient(books, global_trades=trades)
    picked = pick_benign_tickers(client, n=5)
    assert picked == ["KXMLBGAME-A", "KXMLBGAME-B"]  # C dropped (wide), BTC excluded
    assert pick_benign_ticker(client) == "KXMLBGAME-A"  # n=1 wrapper


def test_pick_benign_tickers_series_set_override():
    # series_set membership (politics) overrides the prefix board: match by series ticker.
    books = {"KXGOV-X": _book(0.62, 0.35), "KXMLBGAME-A": _book(0.60, 0.37)}
    trades = [{"ticker": "KXGOV-X"}] * 4 + [{"ticker": "KXMLBGAME-A"}] * 9
    client = FakeClient(books, global_trades=trades)
    picked = pick_benign_tickers(client, series_set={"KXGOV"}, n=5)
    assert picked == ["KXGOV-X"]  # only the political series is eligible


def test_poll_once_fills_and_dedup():
    tk = "KXMLBGAME-A"
    client = FakeClient(
        books={tk: _book(0.60, 0.37)},  # bid 0.60, ask 0.63, mid 0.615
        per_ticker_trades={
            tk: [
                {"trade_id": "t1", "yes_price_dollars": 0.60},  # <= bid -> we BUY
                {"trade_id": "t2", "yes_price_dollars": 0.63},  # >= ask -> we SELL
                {"trade_id": "t3", "yes_price_dollars": 0.615},  # inside -> no fill
            ]
        },
    )
    p = Pilot(ticker=tk)
    poll_once(client, p)
    assert [(f.side, f.price) for f in p.fills] == [(+1, 0.60), (-1, 0.63)]
    assert all(f.mid_at_fill == pytest.approx(0.615) for f in p.fills)
    poll_once(client, p)  # same trade_ids -> deduped, no new fills
    assert len(p.fills) == 2


def test_markout_rows_pools_across_markets():
    # Market A: bought at 0.60 (mid 0.615), mid drifts UP to 0.62 by +15s -> markout +0.5c.
    a = Pilot(ticker="A", mids=[(0.0, 0.615), (15.0, 0.62)],
              fills=[Fill(ts=0.0, side=+1, price=0.60, mid_at_fill=0.615)])
    # Market B: sold at 0.72 (mid 0.71), mid drifts DOWN to 0.70 by +15s -> markout +1.0c.
    b = Pilot(ticker="B", mids=[(0.0, 0.71), (15.0, 0.70)],
              fills=[Fill(ts=0.0, side=-1, price=0.72, mid_at_fill=0.71)])

    rows = markout_rows([a, b])
    assert len(rows) == 1  # only the 15s horizon has a marked mid
    h, n, mean_mark, mean_net = rows[0]
    assert (h, n) == (15, 2)
    assert mean_mark == pytest.approx(0.75)  # +0.75c pooled markout = (+0.5 + +1.0)/2
    assert mean_net == pytest.approx(2.0)    # +2.0c pooled net (edge + markout)

    # Single-market pooling == that market's own numbers (multi-market path is a superset).
    solo = markout_rows([a])
    assert len(solo) == 1
    assert solo[0][:2] == (15, 1)
    assert solo[0][2:] == pytest.approx((0.5, 2.0))


# --- per-ticker activity signal (2026-09-05) --------------------------------------
# The shared /markets/trades tape is a fixed 1000-row window for the WHOLE exchange, so a
# high-frequency series crowds others out of it (measured: KXBTC15M held 322/1000). These
# guard the tape-independent replacement.

class _FakeClient:
    """Minimal KalshiClient stand-in for the activity/enumeration helpers."""

    def __init__(self, trades=None, markets=None):
        self._trades = trades or []
        self._markets = markets or []

    def get(self, path, params=None):
        if path == "/markets/trades":
            tk = (params or {}).get("ticker")
            return {"trades": [t for t in self._trades if t.get("_tk") == tk]}
        if path == "/events":
            # enumerate_candidates queries one SERIES at a time; serve markets whose
            # ticker starts with the requested series.
            ser = (params or {}).get("series_ticker", "")
            mk = [m for m in self._markets if str(m.get("ticker", "")).startswith(ser)]
            return {"events": [{"markets": mk}]} if mk else {"events": []}
        return {}


def _iso(minutes_ago: float) -> str:
    from datetime import UTC, datetime, timedelta
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def test_recent_trade_rate_counts_only_this_ticker_inside_window():
    from core.maker.lp_pilot import recent_trade_rate
    trades = (
        [{"_tk": "A", "created_time": _iso(1)} for _ in range(20)]      # in window
        + [{"_tk": "A", "created_time": _iso(30)} for _ in range(50)]   # too old
        + [{"_tk": "B", "created_time": _iso(1)} for _ in range(99)]    # other market
    )
    c = _FakeClient(trades=trades)
    assert recent_trade_rate(c, "A", window_min=5.0) == 20
    assert recent_trade_rate(c, "B", window_min=5.0) == 99


def test_recent_trade_rate_reads_created_time_not_null_ts():
    """Per-ticker rows carry created_time and a NULL ts — reading ts yields 0 for everything."""
    from core.maker.lp_pilot import recent_trade_rate
    c = _FakeClient(trades=[{"_tk": "A", "ts": None, "created_time": _iso(1)} for _ in range(7)])
    assert recent_trade_rate(c, "A", window_min=5.0) == 7


def test_enumerate_candidates_is_meanrev_two_sided_and_volume_ranked():
    from core.maker.lp_pilot import enumerate_candidates
    mk = lambda tk, bid, vol: {  # noqa: E731
        "ticker": tk, "yes_bid_dollars": bid, "yes_ask_dollars": "0.55",
        "volume_24h_fp": str(vol),
    }
    c = _FakeClient(markets=[
        mk("KXLALIGATOTAL-X-3", "0.53", 9000),    # keep (high vol)
        mk("KXLALIGASPREAD-X-2", "0.53", 1000),   # keep
        mk("KXLALIGAGAME-X-HOME", "0.53", 99999),  # drop: not mean-reverting
        mk("KXLALIGATOTAL-Y-1", "0.53", 10),      # drop: under volume floor
        {"ticker": "KXLALIGATOTAL-Z-1", "yes_bid_dollars": None,
         "yes_ask_dollars": None, "volume_24h_fp": "9999"},  # drop: one-sided
        mk("KXBTC15M-X", "0.53", 99999),          # drop: EXCLUDE crypto
    ])
    out = enumerate_candidates(c, ("KXLALIGA",), min_volume=500.0)
    assert out == ["KXLALIGATOTAL-X-3", "KXLALIGASPREAD-X-2"]


def test_enumerate_candidates_skips_the_game_series_entirely():
    """KX<LEAGUE>GAME is the 3-way match result — directional, never quoted by the maker."""
    from core.maker.lp_pilot import enumerate_candidates
    c = _FakeClient(markets=[{
        "ticker": "KXLALIGAGAME-X-HOME", "yes_bid_dollars": "0.53",
        "yes_ask_dollars": "0.55", "volume_24h_fp": "99999"}])
    assert enumerate_candidates(c, ("KXLALIGA",), min_volume=500.0) == []
