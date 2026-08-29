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

## 2026-08-23 02:20 UTC — paper pilot (soccer)
```
Paper-quoting KXMLSTOTAL-26AUG22NSHCLB-4 for 30 min (poll 4s) ...
  15 sweeps, 93 fills so far
  30 sweeps, 121 fills so far
  45 sweeps, 135 fills so far
  60 sweeps, 167 fills so far
  75 sweeps, 189 fills so far
  90 sweeps, 215 fills so far
  105 sweeps, 313 fills so far
  120 sweeps, 336 fills so far
  135 sweeps, 364 fills so far
  150 sweeps, 382 fills so far
  165 sweeps, 411 fills so far
  180 sweeps, 418 fills so far
  195 sweeps, 426 fills so far
  210 sweeps, 446 fills so far
  225 sweeps, 504 fills so far
  240 sweeps, 563 fills so far
  255 sweeps, 563 fills so far
  270 sweeps, 563 fills so far
  285 sweeps, 563 fills so far
  300 sweeps, 563 fills so far
  315 sweeps, 563 fills so far
  330 sweeps, 563 fills so far
  345 sweeps, 563 fills so far
  360 sweeps, 563 fills so far
  375 sweeps, 563 fills so far
  390 sweeps, 563 fills so far
  405 sweeps, 563 fills so far
  420 sweeps, 563 fills so far
  435 sweeps, 563 fills so far
  450 sweeps, 563 fills so far

======================================================================
PAPER LP PILOT — KXMLSTOTAL-26AUG22NSHCLB-4
======================================================================
ran 15.5 min, 234 polls, avg spread 5.5c
(upper-bound) fills: 563   buys: 330   sells: 233   net inventory: 97

gross edge captured (Σ side·(mid−fill)) : +1225.0c over 563 fills = +2.18c/fill
 horizon  fills w/ mark   mean markout   mean net pnl
-----------------------------------------------------
    15s            544         +0.82c         +3.01c
    30s            504         +0.53c         +2.81c
    60s            461         -0.78c         +1.33c

Read: markout<0 = adverse selection (toxic); net pnl = edge + markout per
fill. POSITIVE net across horizons => a maker plausibly profits here -> a real
Phase-B candidate. Reminder: fill rate is an UPPER BOUND (queue ignored); only
live resting orders (Phase B) give the true rate. Markout is queue-independent.
```

## 2026-08-23 03:40 UTC — paper pilot (soccer)
```
Paper-quoting KXLIGAMXGAME-26AUG22CRAATL-ATL for 30 min (poll 4s) ...
  15 sweeps, 179 fills so far
  30 sweeps, 275 fills so far
  45 sweeps, 385 fills so far
  60 sweeps, 567 fills so far
  75 sweeps, 628 fills so far
  90 sweeps, 834 fills so far
  105 sweeps, 1095 fills so far
  120 sweeps, 1292 fills so far
  135 sweeps, 1468 fills so far
  150 sweeps, 1583 fills so far
  165 sweeps, 1666 fills so far
  180 sweeps, 1749 fills so far
  195 sweeps, 1817 fills so far
  210 sweeps, 1861 fills so far
  225 sweeps, 1913 fills so far
  240 sweeps, 1956 fills so far
  255 sweeps, 1993 fills so far
  270 sweeps, 2044 fills so far
  285 sweeps, 2084 fills so far
  300 sweeps, 2141 fills so far
  315 sweeps, 2177 fills so far
  330 sweeps, 2220 fills so far
  345 sweeps, 2274 fills so far
  360 sweeps, 2312 fills so far
  375 sweeps, 2346 fills so far
  390 sweeps, 2387 fills so far
  405 sweeps, 2430 fills so far
  420 sweeps, 2492 fills so far
  435 sweeps, 2546 fills so far
  450 sweeps, 2603 fills so far

======================================================================
PAPER LP PILOT — KXLIGAMXGAME-26AUG22CRAATL-ATL
======================================================================
ran 29.9 min, 450 polls, avg spread 1.1c
(upper-bound) fills: 2603   buys: 740   sells: 1863   net inventory: -1123

gross edge captured (Σ side·(mid−fill)) : +1507.5c over 2603 fills = +0.58c/fill
 horizon  fills w/ mark   mean markout   mean net pnl
-----------------------------------------------------
    15s           2595         -0.11c         +0.47c
    30s           2580         -0.18c         +0.40c
    60s           2546         -0.49c         +0.09c

Read: markout<0 = adverse selection (toxic); net pnl = edge + markout per
fill. POSITIVE net across horizons => a maker plausibly profits here -> a real
Phase-B candidate. Reminder: fill rate is an UPPER BOUND (queue ignored); only
live resting orders (Phase B) give the true rate. Markout is queue-independent.
```

## 2026-08-23 18:16 UTC — paper pilot (soccer)
```
Paper-quoting KXSERIEASPREAD-26AUG23FROJUV-JUV2 for 30 min (poll 4s) ...
  15 sweeps, 67 fills so far
  30 sweeps, 75 fills so far
  45 sweeps, 86 fills so far
  60 sweeps, 93 fills so far
  75 sweeps, 98 fills so far
  90 sweeps, 106 fills so far
  105 sweeps, 106 fills so far
  120 sweeps, 120 fills so far
  135 sweeps, 124 fills so far
  150 sweeps, 129 fills so far
  165 sweeps, 129 fills so far
  180 sweeps, 130 fills so far
  195 sweeps, 135 fills so far
  210 sweeps, 165 fills so far
  225 sweeps, 178 fills so far
  240 sweeps, 182 fills so far
  255 sweeps, 182 fills so far
  270 sweeps, 182 fills so far
  285 sweeps, 182 fills so far
  300 sweeps, 182 fills so far
  315 sweeps, 182 fills so far
  330 sweeps, 182 fills so far
  345 sweeps, 182 fills so far
  360 sweeps, 182 fills so far
  375 sweeps, 182 fills so far
  390 sweeps, 182 fills so far
  405 sweeps, 182 fills so far
  420 sweeps, 182 fills so far
  435 sweeps, 182 fills so far
  450 sweeps, 182 fills so far

======================================================================
PAPER LP PILOT — KXSERIEASPREAD-26AUG23FROJUV-JUV2
======================================================================
ran 15.5 min, 233 polls, avg spread 5.8c
(upper-bound) fills: 182   buys: 89   sells: 93   net inventory: -4

gross edge captured (Σ side·(mid−fill)) : +550.0c over 182 fills = +3.02c/fill
 horizon  fills w/ mark   mean markout   mean net pnl
-----------------------------------------------------
    15s            181         +1.70c         +4.73c
    30s            178         +1.72c         +4.75c
    60s            172         -0.06c         +2.97c

Read: markout<0 = adverse selection (toxic); net pnl = edge + markout per
fill. POSITIVE net across horizons => a maker plausibly profits here -> a real
Phase-B candidate. Reminder: fill rate is an UPPER BOUND (queue ignored); only
live resting orders (Phase B) give the true rate. Markout is queue-independent.
```

## 2026-08-24 02:19 UTC — paper pilot (soccer)
```
Paper-quoting KXLIGAMXTOTAL-26AUG23PUMNCX-2 for 30 min (poll 4s) ...
  15 sweeps, 60 fills so far
  30 sweeps, 80 fills so far
  45 sweeps, 93 fills so far
  60 sweeps, 104 fills so far
  75 sweeps, 126 fills so far
  90 sweeps, 144 fills so far
  105 sweeps, 163 fills so far
  120 sweeps, 182 fills so far
  135 sweeps, 202 fills so far
  150 sweeps, 225 fills so far
  165 sweeps, 253 fills so far
  180 sweeps, 273 fills so far
  195 sweeps, 306 fills so far
  210 sweeps, 334 fills so far
  225 sweeps, 350 fills so far
  240 sweeps, 364 fills so far
  255 sweeps, 415 fills so far
  270 sweeps, 531 fills so far
  285 sweeps, 617 fills so far
  300 sweeps, 707 fills so far
  315 sweeps, 741 fills so far
  330 sweeps, 791 fills so far
  345 sweeps, 856 fills so far
  360 sweeps, 906 fills so far
  375 sweeps, 971 fills so far
  390 sweeps, 1042 fills so far
  405 sweeps, 1081 fills so far
  420 sweeps, 1098 fills so far
  435 sweeps, 1335 fills so far
  450 sweeps, 1335 fills so far

======================================================================
PAPER LP PILOT — KXLIGAMXTOTAL-26AUG23PUMNCX-2
======================================================================
ran 28.9 min, 435 polls, avg spread 3.6c
(upper-bound) fills: 1335   buys: 664   sells: 671   net inventory: -7

gross edge captured (Σ side·(mid−fill)) : +2589.0c over 1335 fills = +1.94c/fill
 horizon  fills w/ mark   mean markout   mean net pnl
-----------------------------------------------------
    15s           1246         -0.13c         +1.85c
    30s           1190         -0.09c         +1.85c
    60s           1098         -0.02c         +1.69c

Read: markout<0 = adverse selection (toxic); net pnl = edge + markout per
fill. POSITIVE net across horizons => a maker plausibly profits here -> a real
Phase-B candidate. Reminder: fill rate is an UPPER BOUND (queue ignored); only
live resting orders (Phase B) give the true rate. Markout is queue-independent.
```

## 2026-08-24 03:43 UTC — paper pilot (soccer)
```
Paper-quoting KXLIGAMXGAME-26AUG29AMEPUE-AME for 30 min (poll 4s) ...
  15 sweeps, 83 fills so far
  30 sweeps, 83 fills so far
  45 sweeps, 83 fills so far
  60 sweeps, 83 fills so far
  75 sweeps, 83 fills so far
  90 sweeps, 83 fills so far
  105 sweeps, 83 fills so far
  120 sweeps, 83 fills so far
  135 sweeps, 84 fills so far
  150 sweeps, 85 fills so far
  165 sweeps, 85 fills so far
  180 sweeps, 85 fills so far
  195 sweeps, 86 fills so far
  210 sweeps, 86 fills so far
  225 sweeps, 87 fills so far
  240 sweeps, 87 fills so far
  255 sweeps, 87 fills so far
  270 sweeps, 87 fills so far
  285 sweeps, 87 fills so far
  300 sweeps, 87 fills so far
  315 sweeps, 87 fills so far
  330 sweeps, 87 fills so far
  345 sweeps, 87 fills so far
  360 sweeps, 87 fills so far
  375 sweeps, 87 fills so far
  390 sweeps, 87 fills so far
  405 sweeps, 87 fills so far
  420 sweeps, 87 fills so far
  435 sweeps, 87 fills so far
  450 sweeps, 87 fills so far

======================================================================
PAPER LP PILOT — KXLIGAMXGAME-26AUG29AMEPUE-AME
======================================================================
ran 29.9 min, 450 polls, avg spread 1.6c
(upper-bound) fills: 87   buys: 23   sells: 64   net inventory: -41

gross edge captured (Σ side·(mid−fill)) : +87.0c over 87 fills = +1.00c/fill
 horizon  fills w/ mark   mean markout   mean net pnl
-----------------------------------------------------
    15s             87         +0.00c         +1.00c
    30s             87         +0.00c         +1.00c
    60s             87         +0.00c         +1.00c

Read: markout<0 = adverse selection (toxic); net pnl = edge + markout per
fill. POSITIVE net across horizons => a maker plausibly profits here -> a real
Phase-B candidate. Reminder: fill rate is an UPPER BOUND (queue ignored); only
live resting orders (Phase B) give the true rate. Markout is queue-independent.
```

## 2026-08-29 07:34 UTC — paper pilot (soccer)
```
No actively-trading benign market found right now. Pass --ticker.
```
