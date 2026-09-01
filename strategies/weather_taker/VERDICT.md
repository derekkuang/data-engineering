# weather_taker — Kalshi daily-temp ladders (taking + making)

**Status: CLOSED (both angles).**

**Maker angle (first, `weather_logger.py`):** DEAD — maker *pays* ~0.44c on a 1c competed
spread into the guaranteed end-of-day convergence pick-off.

**Taker angle (`calib_study.py`, W0):** NYC/LAX settled ladders vs the BTC-benchmark
calibration harness — the market is well-calibrated (ECE ~2–4%), with residual
miscalibration only in the morning window (up to ~6.5%). Not enough room to clear
spread + taker fee with a forecast/nowcast model; the planned W1–W3 (NOAA pipeline →
edge study → pilot) were not warranted.

**W1-probe — "how do the systematic winners do it?" (`nbm_market_probe.py`, 2026-09-02).**
Web research (docs devlog 2026-09-02) says the profitable systematic play is pricing the
buckets off a calibrated forecast (NBM) and trading when the market deviates by more than
spread+fee — a speed race the loss post-mortems say closes in "seconds" against co-located
bots. Built a persistence probe to measure that claim at OUR latency instead of assuming
it: free Open-Meteo GEFS ensemble (31 members) → empirical daily-high distribution
(fixes the post-mortem's fat-tail/Gaussian bug) → mapped to the live Kalshi ladder →
records every fee-clearing model-vs-market deviation, how large, how long it persists.
**Core logic validated offline** (ensemble spread realistic, bucket parsing correct, model
probs sum to 1.000 = MECE check). **Pending a live intraday-US run** — weather books are
empty overnight, so the probe idles until scheduled in a US afternoon window. Expected
NULL (co-located bots close the window inside our reaction time — the weather twin of the
BRTI tick race); if it instead shows persistent fee-clearing deviations, NBM-proper
(grib2 station percentiles) is the next lift. This reopens ONLY the measurement, not the
CLOSED verdict.

Files here: `calib_study.py`, `h1_spike.py`, `weather_logger.py`, `nbm_market_probe.py`.
