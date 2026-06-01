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


def _load_private_key(path: str) -> RSAPrivateKey:
    """Load an RSA private key from a PEM file (``~`` is expanded)."""
    pem = Path(os.path.expanduser(path)).read_bytes()
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, RSAPrivateKey):
        raise TypeError(f"{path} is not an RSA private key (Kalshi auth needs RSA)")
    return key


class KalshiClient:
    """Signed-or-public GET client. Reads config from env unless passed explicitly."""

    def __init__(
        self,
        *,
        api_base: str | None = None,
        key_id: str | None = None,
        private_key_path: str | None = None,
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
        self.pace_seconds = pace_seconds
        # Sign only if both a key id and a key file are configured; else public mode.
        self._key: RSAPrivateKey | None = None
        if self.key_id and self.private_key_path:
            self._key = _load_private_key(self.private_key_path)
            logger.info("KalshiClient: authenticated mode")
        else:
            logger.info("KalshiClient: public (unauthenticated) mode — market data only")
        self._http = httpx.Client(timeout=timeout)

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
                        path, resp.status_code, backoff, attempt, MAX_RETRIES,
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
