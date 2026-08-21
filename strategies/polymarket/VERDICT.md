# polymarket — spread capture + reward farming on Polymarket

**Status: CLOSED 2026-06-22.**

**Spread capture:** DEAD — competed 1c spreads leave nothing to capture
(`polymarket_navigator.py`).

**Reward farming:** DEAD — the liquidity reward covers ~2.2% of the goal pick-off cost
(`poly_reward_logger.py` → `poly_reward_analyze.py`).

**Takeaway:** Polymarket loses on all three axes vs Kalshi → the maker edge is
**Kalshi-retail-inefficiency-specific**, not a general prediction-market property.
