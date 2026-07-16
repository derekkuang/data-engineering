# Dashboard — Kalshi Opportunity Radar

The serving layer of the platform: a Streamlit view over the dbt mart
`fct_kalshi_opportunity` (the daily Kalshi universe-opportunity snapshot). It shows the
maker-opportunity landscape — where wide retail spreads meet real volume, and which books
are maker-fee-taxed — as KPI tiles, a spread-vs-volume scatter, a category breakdown, and a
sweet-spot table.

```
Athena  fct_kalshi_opportunity ──► publish_snapshot.py ──► data/opportunity_snapshot.parquet ──► app.py (Streamlit)
```

**Offline-first.** `app.py` reads the local `data/opportunity_snapshot.parquet`, so it runs
and deploys with **no AWS credentials**. `publish_snapshot.py` refreshes that file from the
live mart (needs the pipeline user's creds + the `.env` Athena config).

## Run locally

```bash
uv run --group dashboard streamlit run dashboard/app.py
```

## Refresh the data from Athena

```bash
uv run python -m dashboard.publish_snapshot   # re-exports the mart to data/opportunity_snapshot.parquet
```

## Notes

- `spread_capture` is the **gross** half-spread $/day **upper bound** (assumes every fill is
  won) — a *relative ranking* of where to look, not realized P&L and not toxicity-adjusted.
- The scatter's shaded 2–15c band is the makeable retail zone; ~100c points are empty/one-sided
  books (wide ≠ opportunity), ~1c points are crowded/efficient. Orange = maker-fee-taxed.
- Colors are the validated data-viz palette (maker-fee split blue/orange, CVD ΔE 96.7).
