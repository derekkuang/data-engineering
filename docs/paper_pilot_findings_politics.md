# Paper-pilot findings — zero-capital POLITICS-maker toxicity test

Autonomous, **zero-money** test of the politics-maker lead (`ml/research/politics_calibration.py` found the
compression is a GROSS-positive maker edge — favorites underpriced — but gross of news-toxicity + fill-rate +
months of inventory). Each run paper-makes the most-active political favorite against the **live** book
(simulated fills, no capital, no key — public data), measuring realized capture + **markout**.

**What to read:** the **markout** is the key net-question input — how toxic is political-favorite flow (does
the price move against a resting maker after a fill)? If markout is strongly negative → news-toxicity eats the
gross edge (as it did for soccer); if benign → the gross maker edge may survive. Fill *rate* is an optimistic
upper bound (queue ignored). Politics trades slowly, so this **accumulates over many days** — read the trend,
not a single thin session. Scheduled by `.github/workflows/paper-pilot-politics.yml` (1 run/day, 20:00 UTC).
**Newest entries appended below.**

---
