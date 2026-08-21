"""Kalshi weather DIRECTIONAL track — forecast/nowcast-vs-market edge hunt + NOAA pipeline.

The MAKING angle on temp ladders is already dead (measured −0.44c/fill into the end-of-day
convergence pick-off; see strategies/weather_taker/weather_logger.py). This package
tests the TAKING angle:
does a forecast/observation pipeline price the daily-high buckets better than the market does,
by more than the spread + taker fee?

Phases (each gates the next; W0-W2 are read-only, $0):
  W0  calib_study   — is the market miscalibrated anywhere (city x decision-time)? (peer module)
  W1  ingestion.noaa/weather_storage — the NOAA/NWS data layer -> S3 -> dbt fct_weather_pit
  W2  edge_study    — walk-forward, cost-aware verdict on H1 (model) + H2 (nowcast)
  W3  live pilot    — only if W2 clears the fee+spread hurdle with a day-block CI > 0
"""
