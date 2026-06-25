"""Offline unit tests for the WS local order book — snapshot/delta application, top-of-book,
seq-gap detection, and REST<->WS price normalization. No network."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from ingestion.kalshi import KalshiClient
from ingestion.kalshi_ws import KalshiWS, LocalBook, rest_top_of_book


def test_snapshot_loads_and_drops_zero_levels() -> None:
    b = LocalBook()
    b.apply_snapshot(
        {
            "yes_dollars_fp": [["0.50", "100.00"], ["0.49", "50.00"], ["0.30", "0.00"]],
            "no_dollars_fp": [["0.48", "200.00"]],
        },
        seq=1,
    )
    assert b.yes == {"0.50": Decimal("100.00"), "0.49": Decimal("50.00")}  # 0.30 dropped (0 size)
    assert b.seq == 1
    # yes_bid = best yes = 0.50; yes_ask = 1 - best no (0.48) = 0.52
    assert b.top_of_book() == (0.50, 0.52)


def test_delta_add_remove_and_fractional() -> None:
    b = LocalBook()
    b.apply_snapshot(
        {"yes_dollars_fp": [["0.50", "100.00"], ["0.49", "50.00"]],
         "no_dollars_fp": [["0.48", "200.00"]]},
        seq=1,
    )
    b.apply_delta({"side": "no", "price_dollars": "0.51", "delta_fp": "10.00"}, seq=2)
    # -100 -> level hits 0 and is dropped
    b.apply_delta({"side": "yes", "price_dollars": "0.50", "delta_fp": "-100.00"}, seq=3)
    assert "0.50" not in b.yes
    bid, ask = b.top_of_book()
    assert bid == 0.49 and ask == round(1 - 0.51, 4)  # best no now 0.51
    b.apply_delta({"side": "yes", "price_dollars": "0.49", "delta_fp": "-8.89"}, seq=4)
    assert b.yes["0.49"] == Decimal("50.00") - Decimal("8.89")  # fractional, no float drift
    assert b.seq == 4


def test_rest_top_of_book_cents_and_dollars() -> None:
    # REST quotes levels in CENTS -> normalized to dollars
    assert rest_top_of_book({"yes": [[50, 100], [49, 5]], "no": [[48, 200]]}) == (0.50, 0.52)
    # dollars form, nested under 'orderbook'
    assert rest_top_of_book(
        {"orderbook": {"yes_dollars": [["0.50", "100"]], "no_dollars": [["0.48", "200"]]}}
    ) == (0.50, 0.52)


def test_seq_gap_detection() -> None:
    ws = KalshiWS(client=cast(KalshiClient, None))  # _handle doesn't touch the client
    snap: dict[str, Any] = {
        "type": "orderbook_snapshot", "seq": 1,
        "msg": {"market_ticker": "X", "yes_dollars_fp": [["0.50", "10"]], "no_dollars_fp": []},
    }
    ws._handle(snap)
    d2 = {"market_ticker": "X", "side": "yes", "price_dollars": "0.50", "delta_fp": "5"}
    ws._handle({"type": "orderbook_delta", "seq": 2, "msg": d2})
    assert ws._gaps == 0  # in-order
    d5 = {"market_ticker": "X", "side": "yes", "price_dollars": "0.50", "delta_fp": "1"}
    ws._handle({"type": "orderbook_delta", "seq": 5, "msg": d5})  # gap: expected 3
    assert ws._gaps == 1
