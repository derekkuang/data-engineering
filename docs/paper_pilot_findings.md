# Paper-pilot findings — zero-capital live soccer market-making

Autonomous, **zero-money** test of the club-soccer maker edge (`core/maker/lp_paper_pilot.py`, scheduled by
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

## 2026-08-09 02:58 UTC — paper pilot (soccer)
```
No actively-trading benign market found right now. Pass --ticker.
```

## 2026-08-09 04:09 UTC — paper pilot (soccer)
```
No actively-trading benign market found right now. Pass --ticker.
```

## 2026-08-09 18:27 UTC — paper pilot (soccer)
```
Paper-quoting KXELITESERIENTOTAL-26AUG09KBKMFK-2 for 30 min (poll 4s) ...
  15 sweeps, 86 fills so far
  30 sweeps, 86 fills so far
  45 sweeps, 89 fills so far
  60 sweeps, 91 fills so far
  75 sweeps, 91 fills so far
  90 sweeps, 92 fills so far
  105 sweeps, 97 fills so far
  120 sweeps, 105 fills so far
  135 sweeps, 108 fills so far
  150 sweeps, 108 fills so far
  165 sweeps, 141 fills so far
  180 sweeps, 195 fills so far
  195 sweeps, 221 fills so far
  210 sweeps, 227 fills so far
  225 sweeps, 235 fills so far
  240 sweeps, 238 fills so far
  255 sweeps, 249 fills so far
  270 sweeps, 252 fills so far
  285 sweeps, 261 fills so far
  300 sweeps, 265 fills so far
  315 sweeps, 278 fills so far
  330 sweeps, 294 fills so far
  345 sweeps, 319 fills so far
  360 sweeps, 341 fills so far
  375 sweeps, 341 fills so far
  390 sweeps, 341 fills so far
  405 sweeps, 341 fills so far
  420 sweeps, 341 fills so far
  435 sweeps, 341 fills so far
  450 sweeps, 341 fills so far

======================================================================
PAPER LP PILOT — KXELITESERIENTOTAL-26AUG09KBKMFK-2
======================================================================
ran 23.5 min, 341 polls, avg spread 2.3c
(upper-bound) fills: 341   buys: 103   sells: 238   net inventory: -135

gross edge captured (Σ side·(mid−fill)) : +563.0c over 341 fills = +1.65c/fill
 horizon  fills w/ mark   mean markout   mean net pnl
-----------------------------------------------------
    15s            333         +1.85c         +3.53c
    30s            319         +2.80c         +4.52c
    60s            297         +2.81c         +4.41c

Read: markout<0 = adverse selection (toxic); net pnl = edge + markout per
fill. POSITIVE net across horizons => a maker plausibly profits here -> a real
Phase-B candidate. Reminder: fill rate is an UPPER BOUND (queue ignored); only
live resting orders (Phase B) give the true rate. Markout is queue-independent.
```

## 2026-08-10 03:08 UTC — paper pilot (soccer)
```
No actively-trading benign market found right now. Pass --ticker.
```

## 2026-08-10 04:21 UTC — paper pilot (soccer)
```
No actively-trading benign market found right now. Pass --ticker.
```

## 2026-08-22 18:16 UTC — paper pilot (soccer)
```
Paper-quoting KXEPLBTTS-26AUG22BRETOT-BTTS for 30 min (poll 4s) ...
  15 sweeps, 65 fills so far
  30 sweeps, 77 fills so far
  45 sweeps, 84 fills so far
  60 sweeps, 101 fills so far
  75 sweeps, 150 fills so far
  90 sweeps, 151 fills so far
  105 sweeps, 151 fills so far
  120 sweeps, 151 fills so far
  135 sweeps, 151 fills so far
  150 sweeps, 151 fills so far
  165 sweeps, 151 fills so far
  180 sweeps, 151 fills so far
  195 sweeps, 151 fills so far
  210 sweeps, 151 fills so far
  225 sweeps, 151 fills so far
  240 sweeps, 151 fills so far
  255 sweeps, 151 fills so far
  270 sweeps, 151 fills so far
  285 sweeps, 151 fills so far
  300 sweeps, 151 fills so far
  315 sweeps, 151 fills so far
  330 sweeps, 151 fills so far
  345 sweeps, 151 fills so far
  360 sweeps, 151 fills so far
  375 sweeps, 151 fills so far
  390 sweeps, 151 fills so far
  405 sweeps, 151 fills so far
  420 sweeps, 151 fills so far
  435 sweeps, 151 fills so far
  450 sweeps, 151 fills so far

======================================================================
PAPER LP PILOT — KXEPLBTTS-26AUG22BRETOT-BTTS
======================================================================
ran 5.1 min, 77 polls, avg spread 4.2c
(upper-bound) fills: 151   buys: 75   sells: 76   net inventory: -1

gross edge captured (Σ side·(mid−fill)) : +319.5c over 151 fills = +2.12c/fill
 horizon  fills w/ mark   mean markout   mean net pnl
-----------------------------------------------------
    15s            150         -1.05c         +1.06c
    30s            118         -1.14c         +1.01c
    60s            101         -0.90c         +1.51c

Read: markout<0 = adverse selection (toxic); net pnl = edge + markout per
fill. POSITIVE net across horizons => a maker plausibly profits here -> a real
Phase-B candidate. Reminder: fill rate is an UPPER BOUND (queue ignored); only
live resting orders (Phase B) give the true rate. Markout is queue-independent.
```
