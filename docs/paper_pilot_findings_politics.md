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
not a single thin session. Scheduled by `.github/workflows/paper-pilot-politics.yml` (1 run/day, 20:00 UTC).
**Newest entries appended below.**

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

## 2026-08-07 20:48 UTC — paper pilot (politics)
```
No actively-trading benign market found right now. Pass --ticker.
```
