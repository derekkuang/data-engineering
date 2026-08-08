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

## 2026-08-08 18:23 UTC — paper pilot (soccer)
```
Paper-quoting KXUCLWTOTAL-26AUG08SLABRO-1 for 30 min (poll 4s) ...
  15 sweeps, 6 fills so far
  30 sweeps, 6 fills so far
  45 sweeps, 6 fills so far
  60 sweeps, 6 fills so far
  75 sweeps, 6 fills so far
  90 sweeps, 6 fills so far
  105 sweeps, 6 fills so far
  120 sweeps, 6 fills so far
  135 sweeps, 6 fills so far
  150 sweeps, 6 fills so far
  165 sweeps, 6 fills so far
  180 sweeps, 6 fills so far
  195 sweeps, 6 fills so far
  210 sweeps, 6 fills so far
  225 sweeps, 6 fills so far
  240 sweeps, 6 fills so far
  255 sweeps, 6 fills so far
  270 sweeps, 6 fills so far
  285 sweeps, 6 fills so far
  300 sweeps, 6 fills so far
  315 sweeps, 6 fills so far
  330 sweeps, 6 fills so far
  345 sweeps, 6 fills so far
  360 sweeps, 6 fills so far
  375 sweeps, 6 fills so far
  390 sweeps, 6 fills so far
  405 sweeps, 6 fills so far
  420 sweeps, 6 fills so far
  435 sweeps, 6 fills so far
  450 sweeps, 6 fills so far

======================================================================
PAPER LP PILOT — KXUCLWTOTAL-26AUG08SLABRO-1
======================================================================
ran 29.9 min, 450 polls, avg spread 12.0c
(upper-bound) fills: 6   buys: 5   sells: 1   net inventory: 4

gross edge captured (Σ side·(mid−fill)) : +36.0c over 6 fills = +6.00c/fill
 horizon  fills w/ mark   mean markout   mean net pnl
-----------------------------------------------------
    15s              6         +0.00c         +6.00c
    30s              6         +0.00c         +6.00c
    60s              6         +0.00c         +6.00c

Read: markout<0 = adverse selection (toxic); net pnl = edge + markout per
fill. POSITIVE net across horizons => a maker plausibly profits here -> a real
Phase-B candidate. Reminder: fill rate is an UPPER BOUND (queue ignored); only
live resting orders (Phase B) give the true rate. Markout is queue-independent.
```
