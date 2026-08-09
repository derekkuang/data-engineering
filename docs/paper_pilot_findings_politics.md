# Paper-pilot findings — zero-capital POLITICS-maker toxicity test

Autonomous, **zero-money** test of the politics-maker lead (`ml/research/politics_calibration.py` found the
compression is a GROSS-positive maker edge — favorites underpriced — but gross of news-toxicity + fill-rate +
months of inventory). Each run paper-makes the **10 most-active political favorites at once** against the
**live** book (`--markets 10`; simulated fills, no capital, no key — public data), measuring realized
capture + **markout**. Fills pool across markets so the slow politics markout accumulates ~10× faster;
markout is queue-independent so pooling is honest (each fill is still marked vs its own market's mid).

**What to read:** the **markout** is the key net-question input — how toxic is political-favorite flow (does
the price move against a resting maker after a fill)? If markout is strongly negative → news-toxicity eats the
gross edge (as it did for soccer); if benign → the gross maker edge may survive. Fill *rate* is an optimistic
upper bound (queue ignored). Politics trades slowly, so this **accumulates over many days** — read the trend,
not a single thin session. Scheduled by `.github/workflows/paper-pilot-politics.yml` — **two windows/day:
13:00 UTC (midday-ET, reliably several makeable markets → pooling works) + 20:00 UTC (afternoon-ET, often
thin 0-1 makeable but potentially newsier)**. **Newest entries appended below.**

---

## 2026-08-07 — paper pilot (politics) [initial manual run]
```
PAPER LP PILOT — KXDROPOUTPRIMARY-26-MMIL2
======================================================================
ran 11.9 min, 179 polls, avg spread 1.2c
(upper-bound) fills: 96   buys: 16   sells: 80   net inventory: -64

gross edge captured (Σ side·(mid−fill)) : +96.0c over 96 fills = +1.00c/fill
 horizon  fills w/ mark   mean markout   mean net pnl
-----------------------------------------------------
    15s             96         -0.33c         +0.67c
    30s             96         -0.33c         +0.67c
    60s             96         +0.00c         +1.00c

Read: markout<0 = adverse selection (toxic); net pnl = edge + markout per
fill. POSITIVE net across horizons => a maker plausibly profits here -> a real
Phase-B candidate. Reminder: fill rate is an UPPER BOUND (queue ignored); only
live resting orders (Phase B) give the true rate. Markout is queue-independent.
```

## 2026-08-07 12:22 UTC — paper pilot (politics) [first MULTI-MARKET run, manual]
```
PAPER LP PILOT — 5 markets
======================================================================
ran 8.0 min, 170 polls, avg spread 3.8c
(upper-bound) fills: 369   buys: 207   sells: 162   net inventory: 45

gross edge captured (Σ side·(mid−fill)) : +676.0c over 369 fills = +1.83c/fill
 horizon  fills w/ mark   mean markout   mean net pnl
-----------------------------------------------------
    15s            366         +0.03c         +1.72c
    30s            366         +0.02c         +1.71c
    60s            366         +0.02c         +1.71c

                  per-market   fills  net inv  gross/fill
      KXHIPRIMARY-01D26-ECAS      99      -89      +2.54c
     KXPAYROLLS-26JUL-T90000      95       +5      +1.00c
     KXAAAGASW-26AUG10-4.000      89      +71      +2.00c
     KXECONSTATU3-26JUL-T4.2      65      +49      +1.50c
     KXAAAGASD-26AUG08-4.030      21       +9      +2.57c
```
**Read:** markout ≈ 0 (+0.02–0.03c; SE ~±0.1c over 366 fills → **statistically indistinguishable
from zero**; flat across all three horizons) = **no detectable adverse selection** in this window,
and all 5 markets gross-positive. Corroborates the seed's benign markout. **CAVEAT — a calm no-news
window** (8:22 AM ET): the toxicity question is about NEWS shocks (payroll prints / poll drops /
debates / election nights — the analog of the soccer goals that drove soccer's −0.135c), which this
run does not sample; and the picker landed mostly on econ/gas markets (KXPAYROLLS, KXAAAGAS,
KXECONSTAT), which are toxic *precisely* at their scheduled data releases. **First hurdle cleared
(no baseline toxicity in calm conditions), NOT the net verdict** — that needs the newsier windows
the daily runs will accumulate. Multi-market accumulation confirmed working: 369 fills / 8 min vs
the seed's 96 fills / 12 min single-market.

## 2026-08-07 20:48 UTC — paper pilot (politics)
```
No actively-trading benign market found right now. Pass --ticker.
```

## 2026-08-08 20:36 UTC — paper pilot (politics)
```
Paper-quoting KXBRSENMOSTSEATS-26OCT04-PL for 25 min (poll 6s) ...
  15 sweeps, 51 fills so far
  30 sweeps, 51 fills so far
  45 sweeps, 51 fills so far
  60 sweeps, 52 fills so far
  75 sweeps, 52 fills so far
  90 sweeps, 52 fills so far
  105 sweeps, 52 fills so far
  120 sweeps, 52 fills so far
  135 sweeps, 52 fills so far
  150 sweeps, 52 fills so far
  165 sweeps, 52 fills so far
  180 sweeps, 52 fills so far
  195 sweeps, 52 fills so far
  210 sweeps, 52 fills so far
  225 sweeps, 52 fills so far
  240 sweeps, 52 fills so far

======================================================================
PAPER LP PILOT — KXBRSENMOSTSEATS-26OCT04-PL
======================================================================
ran 24.9 min, 250 polls, avg spread 3.0c
(upper-bound) fills: 52   buys: 52   sells: 0   net inventory: 52

gross edge captured (Σ side·(mid−fill)) : +208.0c over 52 fills = +4.00c/fill
 horizon  fills w/ mark   mean markout   mean net pnl
-----------------------------------------------------
    15s             52         -5.47c         -1.47c
    30s             52         -5.47c         -1.47c
    60s             52         -1.55c         +2.45c

Read: markout<0 = adverse selection (toxic); net pnl = edge + markout per
fill. POSITIVE net across horizons => a maker plausibly profits here -> a real
Phase-B candidate. Reminder: fill rate is an UPPER BOUND (queue ignored); only
live resting orders (Phase B) give the true rate. Markout is queue-independent.
```

## 2026-08-09 13:50 UTC — paper pilot (politics)
```
No actively-trading benign market found right now. Pass --ticker.
```

## 2026-08-09 20:39 UTC — paper pilot (politics)
```
Paper-quoting KXHORMUZWEEKLY-26AUG09-T45 for 25 min (poll 6s) ...
  15 sweeps, 96 fills so far
  30 sweeps, 96 fills so far
  45 sweeps, 96 fills so far
  60 sweeps, 96 fills so far
  75 sweeps, 96 fills so far
  90 sweeps, 96 fills so far
  105 sweeps, 96 fills so far
  120 sweeps, 96 fills so far
  135 sweeps, 96 fills so far
  150 sweeps, 96 fills so far
  165 sweeps, 96 fills so far
  180 sweeps, 96 fills so far
  195 sweeps, 96 fills so far
  210 sweeps, 96 fills so far
  225 sweeps, 96 fills so far
  240 sweeps, 96 fills so far

======================================================================
PAPER LP PILOT — KXHORMUZWEEKLY-26AUG09-T45
======================================================================
ran 24.9 min, 250 polls, avg spread 3.0c
(upper-bound) fills: 96   buys: 0   sells: 96   net inventory: -96

gross edge captured (Σ side·(mid−fill)) : +144.0c over 96 fills = +1.50c/fill
 horizon  fills w/ mark   mean markout   mean net pnl
-----------------------------------------------------
    15s             96         +0.00c         +1.50c
    30s             96         +0.00c         +1.50c
    60s             96         +0.00c         +1.50c

Read: markout<0 = adverse selection (toxic); net pnl = edge + markout per
fill. POSITIVE net across horizons => a maker plausibly profits here -> a real
Phase-B candidate. Reminder: fill rate is an UPPER BOUND (queue ignored); only
live resting orders (Phase B) give the true rate. Markout is queue-independent.
```
