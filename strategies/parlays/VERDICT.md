# parlays — MVE parlay mispricing

**Status: CLOSED — bias real, structurally uncapturable.**

`mve_parlay_scan.py`: multivariate-event parlays are mispriced by ~**1.4×** (real bias),
but the book is **buy-only** — you cannot sell/short the overpriced leg combinations, so
the mispricing cannot be captured in either direction. Closed on structure, not on
measurement.
