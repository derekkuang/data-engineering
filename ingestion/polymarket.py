"""Polymarket READ-ONLY market-data client — the shared ingestion layer for the
Polymarket edge-hunt (``strategies/pm_*``).

Public endpoints, no auth/KYC to READ (trading is a separate KYC'd path on the
CFTC-regulated US entity and is out of scope here):

- **Gamma** ``/events`` — the market universe with nested markets, volume, negRisk flag.
- **CLOB** ``POST /books`` — a WHOLE field's order books in ONE round trip. This is the
  load-bearing call: at ~1s RTT from outside the US, fetching a 128-outcome field one
  ``GET /book`` at a time times out; the batch POST fetches it in a single request.
- **CLOB** ``GET /book`` — single-token fallback.

The ONE subtlety the earlier navigator scan got wrong: a Gamma market's
``clobTokenIds[0]`` is ALWAYS the "Yes" leg (it aligns with ``outcomes[0]="Yes"`` and
``outcomePrices[0]``), so summing ``min(ask)`` over each leg's index-0 token IS the
buy-a-full-MECE-basket cost. The apparent "arbs" that fall out of a naive sum are DEAD
books — day-of / pre-settlement fields where every Yes leg is dust (0.0005) and the ask
side is empty. Hence :func:`leg_quote` returns ``None`` for a one-sided/empty book and
:func:`basket_quote` refuses to score a field unless EVERY live leg is two-sided — a
liveness gate, not a token fix.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def _jget(v: Any, default: Any) -> Any:
    """Gamma encodes list fields (outcomes, prices, clobTokenIds) as JSON strings."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return default
    return v if v is not None else default


def client(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, headers={"User-Agent": "crypto-de/pm-research"})


# --------------------------------------------------------------------------- universe


def fetch_events(
    c: httpx.Client, *, limit: int = 250, order: str = "volume24hr"
) -> list[dict[str, Any]]:
    """Top open events (with nested markets) by 24h volume. Gamma paginates at 100."""
    out: list[dict[str, Any]] = []
    offset = 0
    while len(out) < limit:
        r = c.get(
            f"{GAMMA}/events",
            params={
                "closed": "false", "order": order, "ascending": "false",
                "limit": min(100, limit - len(out)), "offset": offset,
            },
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        offset += len(batch)
    return out


def yes_token(market: dict[str, Any]) -> str | None:
    """The market's index-0 ("Yes") CLOB token id, or None if unparseable."""
    toks = _jget(market.get("clobTokenIds"), [])
    return str(toks[0]) if toks else None


# ------------------------------------------------------------------------------ books


@dataclass(frozen=True)
class LegQuote:
    """Top-of-book for one outcome leg. best_bid/best_ask are prices in [0,1];
    bid_size/ask_size are share depths at the touch."""

    token_id: str
    best_bid: float
    best_ask: float
    bid_size: float
    ask_size: float

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid


def leg_quote(book: dict[str, Any]) -> LegQuote | None:
    """Top-of-book from a CLOB book payload, or None if the book is one-sided/empty
    (the liveness gate: a dead leg has no makeable/arbable price)."""
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None
    bb = max(bids, key=lambda o: float(o["price"]))
    ba = min(asks, key=lambda o: float(o["price"]))
    return LegQuote(
        token_id=str(book.get("asset_id", "")),
        best_bid=float(bb["price"]), best_ask=float(ba["price"]),
        bid_size=float(bb["size"]), ask_size=float(ba["size"]),
    )


def fetch_books(c: httpx.Client, token_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Order books for many tokens in ONE POST /books round trip -> {token_id: book}.
    The batch call is what makes a 128-outcome field scannable at high RTT."""
    if not token_ids:
        return {}
    r = c.post(f"{CLOB}/books", json=[{"token_id": t} for t in token_ids])
    r.raise_for_status()
    books: list[dict[str, Any]] = r.json()
    out: dict[str, dict[str, Any]] = {str(b.get("asset_id", "")): b for b in books}
    return out


def fetch_book(c: httpx.Client, token_id: str) -> dict[str, Any]:
    """Single-token order book (GET /book) — fallback / spot checks."""
    r = c.get(f"{CLOB}/book", params={"token_id": token_id})
    r.raise_for_status()
    book: dict[str, Any] = r.json()
    return book


# ---------------------------------------------------------------- MECE basket scoring


@dataclass(frozen=True)
class BasketQuote:
    """A NegRisk MECE field scored for complement (basket) consistency. On a
    mutually-exclusive-and-exhaustive field the Yes legs must sum to 1:

    - ``sum_ask`` = cost to BUY one Yes in every outcome (guaranteed $1 payout).
      ``sum_ask < 1`` is a risk-free BUY-basket arb of size ``1 - sum_ask`` per set.
    - ``sum_bid`` = proceeds to SELL one Yes in every outcome. ``sum_bid > 1`` is a
      SELL-basket arb of ``sum_bid - 1`` per set.

    ``min_ask_size`` / ``min_bid_size`` are the binding executable depth (the arb is
    capped by the thinnest leg). Scored only when EVERY live leg is two-sided."""

    slug: str
    n_outcomes: int
    volume_24h: float
    sum_ask: float
    sum_bid: float
    min_ask_size: float
    min_bid_size: float

    @property
    def buy_edge(self) -> float:
        return 1.0 - self.sum_ask

    @property
    def sell_edge(self) -> float:
        return self.sum_bid - 1.0


def basket_quote(
    c: httpx.Client, event: dict[str, Any]
) -> BasketQuote | None:
    """Score one Gamma event's MECE field for basket-arb consistency, or None if it
    isn't a scorable field (not negRisk, <3 live legs, or any leg's book is one-sided
    — the liveness gate that rejects dead day-of ladders)."""
    if not event.get("negRisk"):
        return None
    markets = [m for m in (event.get("markets") or []) if not m.get("closed")]
    tokens = [t for m in markets if (t := yes_token(m))]
    if len(tokens) < 3:
        return None
    books = fetch_books(c, tokens)
    quotes = [leg_quote(books[t]) for t in tokens if t in books]
    if len(quotes) < 3 or any(q is None for q in quotes):
        return None  # a one-sided leg -> dead field, not an arb
    qs = [q for q in quotes if q is not None]
    return BasketQuote(
        slug=str(event.get("slug", "")),
        n_outcomes=len(qs),
        volume_24h=float(event.get("volume24hr") or 0.0),
        sum_ask=sum(q.best_ask for q in qs),
        sum_bid=sum(q.best_bid for q in qs),
        min_ask_size=min(q.ask_size for q in qs),
        min_bid_size=min(q.bid_size for q in qs),
    )
