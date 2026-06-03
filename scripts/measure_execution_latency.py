"""Where would we sit on the edge-vs-latency curve? An execution-speed go/no-go.

The live-execution reconciliation (ml/live_exec_reconcile.py) showed the +8%
KXBTC15M edge has a ~20s half-life: the book reprices ~0.19c/s, ROI ~+7% at 1s,
~+5% at 10s, breakeven ~30s. So the open question is purely "how fast could we
execute?". This measures the pieces of a real decision->order loop that we CAN
measure without an account or any money:

  * Kalshi market-data GET round-trip — the network proxy for an order POST (an
    order submission is the same HTTPS round-trip plus a small server-side match).
  * Coinbase spot GET round-trip — the live spot/feature source.
  * Model inference — features -> probability for one window (a tiny logistic).

A real loop fetches data (Kalshi book + Coinbase spot, in parallel), computes the
signal, then submits an order — so the estimate is:
    max(kalshi_md, coinbase) + inference + order_submit(~= one Kalshi round-trip).

NOT measured (only a live test can): real order-ack time, fill probability, queue
position, slippage at size. And this runs from a residential machine = an UPPER
bound; a cloud box in Kalshi's region (us-east-1) would be materially faster.

Usage:
    uv run python scripts/measure_execution_latency.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx
import numpy as np
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.kalshi import SERIES_BTC_15M, KalshiClient  # noqa: E402
from ingestion.kalshi_orderbook import active_market  # noqa: E402
from ml.model import logistic_pipeline  # noqa: E402

COINBASE_TICKER = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
N_SAMPLES = 25
N_FEATURES = 25  # ~feature count of fct_btc_15min_training; inference cost is shape-driven
DRIFT_C_PER_S = 3.8 / 20.0  # measured: ~3.8c mid drift per 20s (live_exec_reconcile.py)
BREAKEVEN_LATENCY_S = 30.0  # from the curve: edge hits ~0 near 30s of added latency


def _time_get(url: str, params: dict[str, str | int] | None, n: int, gap: float) -> np.ndarray:
    """Time n GET round-trips (ms), spaced by `gap`s to respect rate limits. A fresh
    client per call avoids connection reuse flattering the number (each is cold)."""
    out: list[float] = []
    for i in range(n):
        try:
            with httpx.Client(timeout=10.0) as http:
                t0 = time.perf_counter()
                resp = http.get(url, params=params)
                resp.raise_for_status()
                out.append((time.perf_counter() - t0) * 1000.0)
        except httpx.HTTPError as exc:
            print(f"  (sample {i + 1} failed: {type(exc).__name__})", file=sys.stderr)
        if i < n - 1:
            time.sleep(gap)
    return np.array(out)


def _pct(label: str, ms: np.ndarray) -> None:
    if ms.size == 0:
        print(f"{label:<28}  no successful samples")
        return
    print(
        f"{label:<28}{ms.size:>5}{np.median(ms):>10.1f}{np.percentile(ms, 90):>10.1f}"
        f"{np.percentile(ms, 99):>10.1f}{ms.min():>10.1f}"
    )


def _inference_ms() -> np.ndarray:
    """Time features->probability for one window. Fit on synthetic data of the real
    shape (the fit is offline/once in production, so only predict is on the path)."""
    rng = np.random.default_rng(0)
    x = rng.standard_normal((200, N_FEATURES))
    y = (rng.random(200) > 0.5).astype(int)
    model = logistic_pipeline()
    model.fit(x, y)
    one = x[:1]
    samples = []
    for _ in range(2000):
        t0 = time.perf_counter()
        model.predict_proba(one)
        samples.append((time.perf_counter() - t0) * 1000.0)
    return np.array(samples)


def main() -> int:
    load_dotenv()
    api_base = os.environ.get("KALSHI_API_BASE", "").rstrip("/")
    if not api_base:
        print("FAIL: KALSHI_API_BASE not set.", file=sys.stderr)
        return 1

    client = KalshiClient()
    try:
        market = active_market(client)
    finally:
        client.close()

    # Prefer the orderbook (what you'd read right before ordering); fall back to the
    # markets list when we're between 15-min windows and nothing is open.
    if market is not None:
        kalshi_url = f"{api_base}/markets/{market['ticker']}/orderbook"
        kalshi_params: dict[str, str | int] = {"depth": 50}
        kalshi_label = "Kalshi GET /orderbook"
    else:
        kalshi_url = f"{api_base}/markets"
        kalshi_params = {"series_ticker": SERIES_BTC_15M, "limit": 1}
        kalshi_label = "Kalshi GET /markets"
        print("(no open KXBTC15M window right now — timing /markets instead)\n")

    print(f"Sampling {N_SAMPLES}x each (residential machine = UPPER bound on latency)\n")
    print(f"{'round-trip (ms)':<28}{'n':>5}{'median':>10}{'p90':>10}{'p99':>10}{'min':>10}")
    print("-" * 73)
    kalshi_ms = _time_get(kalshi_url, kalshi_params, N_SAMPLES, gap=0.6)
    _pct(kalshi_label, kalshi_ms)
    coinbase_ms = _time_get(COINBASE_TICKER, None, N_SAMPLES, gap=0.3)
    _pct("Coinbase GET /ticker", coinbase_ms)
    infer_ms = _inference_ms()
    _pct("model inference (1 row)", infer_ms)

    if kalshi_ms.size == 0:
        print("\nFAIL: no Kalshi samples; cannot estimate.", file=sys.stderr)
        return 1

    # End-to-end decision->order estimate (medians): data fetch is parallel, so take
    # the slower of the two reads; add inference; add one Kalshi round-trip to submit.
    k_med = float(np.median(kalshi_ms))
    c_med = float(np.median(coinbase_ms)) if coinbase_ms.size else 0.0
    fetch = max(k_med, c_med)
    submit = k_med
    e2e_ms = fetch + float(np.median(infer_ms)) + submit
    e2e_s = e2e_ms / 1000.0
    added_cost_c = e2e_s * DRIFT_C_PER_S

    print("\nEstimated decision->order latency (medians):")
    print(f"  data fetch (parallel, slower of the two)  {fetch:>8.1f} ms")
    print(f"  model inference                           {float(np.median(infer_ms)):>8.3f} ms")
    print(f"  order submit (~one round-trip)            {submit:>8.1f} ms")
    print(f"  END-TO-END                                {e2e_ms:>8.1f} ms  (~{e2e_s:.2f}s)")
    headroom = BREAKEVEN_LATENCY_S / e2e_s if e2e_s > 0 else float("inf")
    if e2e_s < BREAKEVEN_LATENCY_S:
        verdict = (
            f"{e2e_s:.2f}s is inside the ~{BREAKEVEN_LATENCY_S:.0f}s breakeven ({headroom:.0f}x "
            "headroom),\n   so most of the edge would survive THIS latency."
        )
    else:
        verdict = (
            f"{e2e_s:.2f}s EXCEEDS the ~{BREAKEVEN_LATENCY_S:.0f}s breakeven — the edge is "
            "gone at this latency."
        )
    print(
        f"\n=> ~{added_cost_c:.2f}c of repricing cost at this latency "
        f"(book moves ~{DRIFT_C_PER_S * 100:.1f}c/s)."
    )
    print(f"   On the curve (ml/live_exec_reconcile.py): {verdict}")
    print(
        "   Caveats: residential (cloud would be faster) and GET-as-order-proxy; real\n"
        "   fills / slippage at size are unproven — that's the live execution test."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
