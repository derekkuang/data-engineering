# Skew A/B findings — is the club-soccer maker doing TWO-SIDED capture?

Autonomous zero-money results from `.github/workflows/skew-ab.yml`
(`strategies/soccer_mm/skew_ab.py`), newest appended at the bottom.

**What this measures.** One book+trades fetch per ticker per sweep is handed to BOTH arms, so
they see identical markets, prints and timestamps — the only difference is inventory skew
(`lp_live`'s rule vs none). So any divergence is attributable to skew alone, not to
scoreline or volatility.

**Read in this order:**
1. **PEGGED / mean |inv|** — is inventory genuinely two-sided, or pinned at the cap?
2. **net/fill by horizon** — does capture survive adverse selection (esp. at 60s)?
3. **markout** — the queue-INDEPENDENT toxicity signal.

**Caveat that never goes away:** fill counts are an optimistic UPPER bound (queue position is
unknowable on paper). Markout and the inventory path are the trustworthy outputs. Only real
resting orders settle the fill rate.

**Baseline to reproduce** (2026-09-06, 3 markets / 9 min, ad-hoc run):
SKEW ON — PEGGED 0/3, mean |inv| 1.3, net +2.02c @30s / +1.95c @60s, +$2.46 on 144 fills.
SKEW OFF — PEGGED 3/3, mean |inv| 10.0, net +0.74c @30s / **-0.01c @60s**, +$1.41 on 266 fills.
One session is a promising read, NOT a fact — this stream exists to test reproducibility.

---
