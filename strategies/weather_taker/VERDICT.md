# weather_taker — Kalshi daily-temp ladders (taking + making)

**Status: CLOSED (both angles).**

**Maker angle (first, `weather_logger.py`):** DEAD — maker *pays* ~0.44c on a 1c competed
spread into the guaranteed end-of-day convergence pick-off.

**Taker angle (`calib_study.py`, W0):** NYC/LAX settled ladders vs the BTC-benchmark
calibration harness — the market is well-calibrated (ECE ~2–4%), with residual
miscalibration only in the morning window (up to ~6.5%). Not enough room to clear
spread + taker fee with a forecast/nowcast model; the planned W1–W3 (NOAA pipeline →
edge study → pilot) were not warranted.

Files here: `calib_study.py`, `h1_spike.py`, `weather_logger.py`.
