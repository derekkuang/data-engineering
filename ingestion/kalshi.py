"""Kalshi API client + candlestick normalization (public, read-only).

Kalshi's market-data endpoints (markets, candlesticks) are PUBLIC on prod, so no
auth is required. This pipeline only reads price/implied-prob; it never places
orders. The RSA-PSS signer is kept for any future authenticated endpoint but is
optional — if no key is configured, the client runs unauthenticated.

Auth contract (if used): sign ``timestamp_ms + HTTP_METHOD + path`` (path WITHOUT
query string) with the account RSA private key via RSA-PSS (SHA-256, MGF1, salt
length = digest length), base64 the signature, send KALSHI-ACCESS-{KEY,SIGNATURE,
TIMESTAMP} headers.

Instrument: series ``KXBTC15M`` = "BTC price up in next 15 mins?" — a binary
market whose price (0..1 dollars) IS the implied probability of an up move. Each
settled market is one 15-min window; its candlesticks are the implied-prob path,
with yes_bid/yes_ask = the trading spread (cost) and the market ``result`` = the
up/down outcome (the label).
"""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30.0
RATE_LIMIT_SLEEP_SECONDS = 0.5  # the public API rate-limits; pace every request
MAX_RETRIES = 6
SERIES_BTC_15M = "KXBTC15M"


# Env vars that may hold the private key PEM INLINE (pasted into .env), checked in
# order. Alternatively KALSHI_PRIVATE_KEY_PATH points to a PEM file.
INLINE_KEY_ENV_VARS = ("KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PEM", "KALSHI_API_PRIVATE_KEY")


def _pem_to_key(pem: bytes) -> RSAPrivateKey:
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, RSAPrivateKey):
        raise TypeError("configured key is not an RSA private key (Kalshi auth needs RSA)")
    return key


def _load_private_key(path: str) -> RSAPrivateKey:
    """Load an RSA private key from a PEM file (``~`` is expanded)."""
    return _pem_to_key(Path(os.path.expanduser(path)).read_bytes())


def _load_private_key_pem(pem: str) -> RSAPrivateKey:
    """Load an RSA private key from an inline PEM string (e.g. pasted into .env).
    Tolerates a single-line PEM whose newlines were flattened to literal ``\\n``."""
    text = pem.strip()
    if "-----BEGIN" in text and "\n" not in text:  # flattened single-line PEM
        text = text.replace("\\n", "\n")
    return _pem_to_key(text.encode())


class KalshiClient:
    """Signed-or-public GET client. Reads config from env unless passed explicitly."""

    def __init__(
        self,
        *,
        api_base: str | None = None,
        key_id: str | None = None,
        private_key_path: str | None = None,
        private_key_pem: str | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        pace_seconds: float = RATE_LIMIT_SLEEP_SECONDS,
    ) -> None:
        self.api_base = (api_base or os.environ["KALSHI_API_BASE"]).rstrip("/")
        self.key_id = key_id if key_id is not None else os.environ.get("KALSHI_API_KEY_ID", "")
        self.private_key_path = (
            private_key_path
            if private_key_path is not None
            else os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        )
        # Inline PEM (pasted into .env) takes priority over a key file. Accept a few
        # common var names so it works regardless of exactly what the user named it.
        inline_pem = private_key_pem
        if inline_pem is None:
            for var in INLINE_KEY_ENV_VARS:
                if os.environ.get(var, "").strip():
                    inline_pem = os.environ[var]
                    break
        self.pace_seconds = pace_seconds
        # Auth needs a key id AND a private key. Prefer an inline PEM; if it is absent
        # OR fails to parse (e.g. an unquoted multiline paste in .env), fall back to a
        # key file. Else public mode.
        self._key: RSAPrivateKey | None = None
        if self.key_id and inline_pem and inline_pem.strip():
            try:
                self._key = _load_private_key_pem(inline_pem)
                logger.info("KalshiClient: authenticated mode (inline key)")
            except (ValueError, TypeError) as exc:
                logger.warning("inline key did not parse (%s); falling back to key file", exc)
        if self._key is None and self.key_id and self.private_key_path:
            self._key = _load_private_key(self.private_key_path)
            logger.info("KalshiClient: authenticated mode (key file)")
        if self._key is None:
            logger.info("KalshiClient: public (unauthenticated) mode — market data only")
        self._http = httpx.Client(timeout=timeout)

    @property
    def authenticated(self) -> bool:
        """True if a signing key is loaded (writes/portfolio endpoints are usable)."""
        return self._key is not None

    # --- signing -----------------------------------------------------------
    def _signed_headers(self, method: str, url: str) -> dict[str, str]:
        assert self._key is not None, "signing requested without a loaded private key"
        ts = str(int(time.time() * 1000))  # milliseconds since epoch
        path = urlparse(url).path  # path only — no host, no query string
        message = f"{ts}{method.upper()}{path}".encode()
        signature = self._key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    def ws_handshake(self) -> tuple[str, dict[str, str]]:
        """(ws_url, signed handshake headers) for the WebSocket API. Reuses the same
        RSA-PSS signer as REST (signs ``ts + "GET" + "/trade-api/ws/v2"``). The WS feed
        requires auth even for market data, so a signing key MUST be loaded. The WS host
        is the REST host with the ``-ws`` suffix and the ``/ws/`` path segment."""
        if self._key is None:
            raise RuntimeError("WebSocket requires authentication — load a signing key")
        ws_url = (
            self.api_base.replace("https://", "wss://")
            .replace("http://", "ws://")
            .replace("external-api.", "external-api-ws.")
            .replace("/trade-api/v2", "/trade-api/ws/v2")
        )
        return ws_url, self._signed_headers("GET", ws_url)

    # --- transport ---------------------------------------------------------
    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Signed/public GET with retry + backoff on 429/5xx. ``path`` starts with ``/``."""
        url = f"{self.api_base}{path}"
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                headers = self._signed_headers("GET", url) if self._key is not None else None
                resp = self._http.get(url, params=params, headers=headers)
                if resp.status_code == 429 or resp.status_code >= 500:
                    backoff = RATE_LIMIT_SLEEP_SECONDS * 2**attempt
                    logger.warning(
                        "GET %s -> %s, backing off %.2fs (attempt %d/%d)",
                        path,
                        resp.status_code,
                        backoff,
                        attempt,
                        MAX_RETRIES,
                    )
                    time.sleep(backoff)
                    continue
                resp.raise_for_status()
                time.sleep(self.pace_seconds)  # pace successful calls to stay under the limit
                payload: dict[str, Any] = resp.json()
                return payload
            except httpx.HTTPError as exc:
                last_error = exc
                time.sleep(RATE_LIMIT_SLEEP_SECONDS * 2**attempt)
        raise RuntimeError(f"Kalshi GET {path} failed after {MAX_RETRIES} attempts") from last_error

    def _write(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Signed POST/DELETE with retry/backoff. AUTH IS REQUIRED (no public writes).
        Kalshi signs ``timestamp + METHOD + path`` only — the JSON body is NOT signed,
        so the same RSA-PSS header scheme as GET applies."""
        if self._key is None:
            raise RuntimeError(f"{method} {path} requires authentication — no key configured")
        url = f"{self.api_base}{path}"
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                headers = self._signed_headers(method, url)
                resp = self._http.request(method, url, json=body, headers=headers)
                if resp.status_code == 429 or resp.status_code >= 500:
                    time.sleep(RATE_LIMIT_SLEEP_SECONDS * 2**attempt)
                    continue
                resp.raise_for_status()
                time.sleep(self.pace_seconds)
                payload: dict[str, Any] = resp.json() if resp.content else {}
                return payload
            except httpx.HTTPStatusError:
                raise  # 4xx (e.g. rejected order) is informative — don't retry/swallow it
            except httpx.HTTPError as exc:
                last_error = exc
                time.sleep(RATE_LIMIT_SLEEP_SECONDS * 2**attempt)
        raise RuntimeError(
            f"Kalshi {method} {path} failed after {MAX_RETRIES} attempts"
        ) from last_error

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._write("POST", path, body)

    def delete(self, path: str) -> dict[str, Any]:
        return self._write("DELETE", path)

    # --- endpoints ---------------------------------------------------------
    def get_exchange_status(self) -> dict[str, Any]:
        return self.get("/exchange/status")

    def get_balance(self) -> dict[str, Any]:
        """Requires auth — used only to prove signing when a key is configured."""
        return self.get("/portfolio/balance")

    def get_markets(self, **params: Any) -> dict[str, Any]:
        return self.get("/markets", params=params or None)

    def list_markets(
        self,
        series_ticker: str,
        *,
        status: str | None = None,
        min_close_ts: int | None = None,
        max_close_ts: int | None = None,
        page_limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """All markets for a series (optionally filtered by status + close-time
        window), following the cursor across pages."""
        base_params: dict[str, Any] = {"series_ticker": series_ticker, "limit": page_limit}
        if status:
            base_params["status"] = status
        if min_close_ts is not None:
            base_params["min_close_ts"] = min_close_ts
        if max_close_ts is not None:
            base_params["max_close_ts"] = max_close_ts

        markets: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params = dict(base_params)
            if cursor:
                params["cursor"] = cursor
            resp = self.get("/markets", params=params)
            markets.extend(resp.get("markets", []))
            cursor = resp.get("cursor") or None
            if not cursor:
                break
        return markets

    def get_market_candlesticks(
        self, series_ticker: str, ticker: str, start_ts: int, end_ts: int, period_interval: int = 1
    ) -> list[dict[str, Any]]:
        """1-min (or 60/1440) OHLC of a market's price — the implied-prob path.
        ``start_ts``/``end_ts`` are Unix seconds."""
        path = f"/series/{series_ticker}/markets/{ticker}/candlesticks"
        resp = self.get(
            path,
            params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )
        candles: list[dict[str, Any]] = resp.get("candlesticks", [])
        return candles

    def get_market_candlesticks_batch(
        self, market_tickers: list[str], start_ts: int, end_ts: int, period_interval: int = 1
    ) -> dict[str, list[dict[str, Any]]]:
        """Candlesticks for many markets in ONE call (Kalshi allows up to 100 tickers
        and 10,000 candlesticks per request), chunked at 100. Returns
        ``{market_ticker: candlesticks}``.

        CAUTION — the ~10k-candlestick budget is ``n_tickers × range_minutes``, NOT
        the number of bars that actually exist: a whole-day range (1,440 min) with
        96 tickers is rejected with a 400 even though only ~1,500 bars would return.
        Callers must keep ``len(chunk) × minutes(start_ts..end_ts)`` under ~10k —
        e.g. group consecutive 15-min windows so the shared range stays tight (see
        ml/live_cluster_verdict.py's greedy chunking)."""
        out: dict[str, list[dict[str, Any]]] = {}
        for i in range(0, len(market_tickers), 100):
            chunk = market_tickers[i : i + 100]
            resp = self.get(
                "/markets/candlesticks",
                params={
                    "market_tickers": ",".join(chunk),
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "period_interval": period_interval,
                },
            )
            for entry in resp.get("markets", []):
                out[entry["market_ticker"]] = entry.get("candlesticks", [])
        return out

    def get_market_orderbook(self, ticker: str, depth: int | None = None) -> dict[str, Any]:
        """Live resting order book for a market. Read-only market data (works in
        public mode). ``depth`` caps the number of price levels per side. Unlike
        candlesticks, this is a live-only snapshot — there is no historical
        order-book endpoint.

        Kalshi returns ``orderbook_fp`` with ``yes_dollars`` / ``no_dollars`` arrays
        of [price_dollars, quantity] (strings). A 'no' bid at price p is the same as
        a 'yes' ask at (1 - p), so best_yes_ask = 1 - best_no_bid."""
        params = {"depth": depth} if depth is not None else None
        resp = self.get(f"/markets/{ticker}/orderbook", params=params)
        # Explicit None checks (not `or`) so a present-but-EMPTY book ({}) — a market
        # with no resting orders — isn't masked by falling through to the other key.
        book = resp.get("orderbook_fp")
        if book is None:
            book = resp.get("orderbook")
        result: dict[str, Any] = book if book is not None else {}
        return result

    # --- perpetual futures (margin) market data ----------------------------
    # Perps live on a SEPARATE API surface under /margin (same host, public for
    # market data). Crypto perps settle/fund on the SAME CF Benchmarks RTI that
    # KXBTC15M settles on (BTC = BRTI), so the perp is a live, tradeable proxy
    # for the binary's settlement index. Price is quoted in dollars per contract
    # = index x contract_size (BTC contract_size = 0.0001 -> BTC = price / 0.0001).
    def list_margin_markets(self, *, status: str | None = None) -> list[dict[str, Any]]:
        """All perpetual-futures markets (optionally filtered by status)."""
        params = {"status": status} if status else None
        resp = self.get("/margin/markets", params=params)
        markets: list[dict[str, Any]] = resp.get("markets", [])
        return markets

    def get_margin_market(self, ticker: str) -> dict[str, Any]:
        resp = self.get(f"/margin/markets/{ticker}")
        market: dict[str, Any] = resp.get("market", resp)
        return market

    def get_margin_candlesticks(
        self, ticker: str, start_ts: int, end_ts: int, period_interval: int = 1
    ) -> list[dict[str, Any]]:
        """OHLC of a perp's traded price (bid/ask/price), Unix-second range.
        ``period_interval`` is minutes (1/60/1440)."""
        resp = self.get(
            f"/margin/markets/{ticker}/candlesticks",
            params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )
        candles: list[dict[str, Any]] = resp.get("candlesticks", [])
        return candles

    def get_margin_orderbook(self, ticker: str) -> dict[str, Any]:
        """Live resting order book for a perp (read-only market data)."""
        resp = self.get(f"/margin/markets/{ticker}/orderbook")
        book: dict[str, Any] = resp.get("orderbook", resp)
        return book

    def get_funding_rates_historical(
        self, ticker: str | None = None, start_ts: int | None = None, end_ts: int | None = None
    ) -> list[dict[str, Any]]:
        """Historical 8-hourly funding rates (capped +/-2%/period). All params
        optional; omit ``ticker`` to span all perps."""
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if start_ts is not None:
            params["start_ts"] = start_ts
        if end_ts is not None:
            params["end_ts"] = end_ts
        resp = self.get("/margin/funding_rates/historical", params=params or None)
        rates: list[dict[str, Any]] = resp.get("funding_rates", [])
        return rates

    def close(self) -> None:
        self._http.close()


# --- normalization ---------------------------------------------------------
@dataclass(frozen=True)
class KalshiCandle:
    """One 1-minute candlestick for a 15-min BTC market, flattened for storage."""

    market_ticker: str
    series_ticker: str
    window_open_at: datetime
    window_close_at: datetime
    event_at: datetime
    implied_prob_open: float | None
    implied_prob_high: float | None
    implied_prob_low: float | None
    implied_prob_close: float | None
    implied_prob_mean: float | None
    yes_bid_close: float | None
    yes_ask_close: float | None
    volume: float
    open_interest: float
    result: str  # "yes" | "no" | "" (forward-looking label; kept out of the PIT mart)


def _f(d: dict[str, Any] | None, key: str) -> float | None:
    """Parse an optional dollar string into a float (Kalshi sends strings)."""
    val = (d or {}).get(key)
    return float(val) if val is not None else None


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def normalize_market_candles(
    market: dict[str, Any], candlesticks: list[dict[str, Any]], series_ticker: str
) -> list[KalshiCandle]:
    """Flatten a market + its candlesticks into KalshiCandle rows."""
    market_ticker = market["ticker"]
    window_open = _parse_iso(market["open_time"])
    window_close = _parse_iso(market["close_time"])
    result = market.get("result") or ""

    rows: list[KalshiCandle] = []
    for cs in candlesticks:
        price = cs.get("price") or {}
        rows.append(
            KalshiCandle(
                market_ticker=market_ticker,
                series_ticker=series_ticker,
                window_open_at=window_open,
                window_close_at=window_close,
                event_at=datetime.fromtimestamp(cs["end_period_ts"], tz=UTC),
                implied_prob_open=_f(price, "open_dollars"),
                implied_prob_high=_f(price, "high_dollars"),
                implied_prob_low=_f(price, "low_dollars"),
                implied_prob_close=_f(price, "close_dollars"),
                implied_prob_mean=_f(price, "mean_dollars"),
                yes_bid_close=_f(cs.get("yes_bid"), "close_dollars"),
                yes_ask_close=_f(cs.get("yes_ask"), "close_dollars"),
                volume=float(cs.get("volume_fp") or 0.0),
                open_interest=float(cs.get("open_interest_fp") or 0.0),
                result=result,
            )
        )
    return rows
