# Paper-pilot findings — zero-capital live soccer market-making

Autonomous, **zero-money** test of the club-soccer maker edge (`ml/lp/lp_paper_pilot.py`, scheduled by
`.github/workflows/paper-pilot.yml`). Each run quotes a maker at the touch against the **live** Kalshi
book and **simulates** fills (no capital, no trading key — public market data only), measuring realized
capture + markout at seconds resolution on whatever club-soccer game is most active at the time.

**How to read it — the honest boundary:** the fill *rate* is an **optimistic upper bound** (queue
position is unknowable on paper), so this **de-risks the real pilot but cannot CONFIRM** the edge. The
**markout** (adverse selection / toxicity) is queue-independent and therefore **trustworthy**. So:
- `net pnl > 0` across horizons on real fills = a plausible edge → a real-money-pilot candidate.
- `net pnl < 0` even here (optimistic fills) = kill it cheaply; the real book would be worse.
- `markout << 0` = toxic (goals pick us off) regardless of fill rate.

Runs at weekend club-soccer windows (Americas evenings + weekend afternoons, UTC). A run that finds no
live/makeable soccer market logs that and exits. **Newest entries appended below.**

---

## 2026-08-08 02:51 UTC — paper pilot (soccer)
```
No actively-trading benign market found right now. Pass --ticker.
```

## 2026-08-08 04:02 UTC — paper pilot (soccer)
```
No actively-trading benign market found right now. Pass --ticker.
```
