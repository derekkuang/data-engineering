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

## 2026-09-06 15:24 UTC — skew A/B (soccer)
```
GET /events -> 429, backing off 1.00s (attempt 1/6)
GET /events -> 429, backing off 1.00s (attempt 1/6)
GET /events -> 429, backing off 1.00s (attempt 1/6)
SKEW A/B on 1 markets for 2 min (skew 0.01 vs 0.0), identical inputs to both arms:
   KXLALIGASPREAD-26SEP06VCFBAR-BAR4
  20 sweeps | pegged: skew 0/1, flat 1/1

==============================================================================
SKEW A/B — 1 markets, 20 sweeps, identical inputs
==============================================================================

--- SKEW ON (0.01/contract) = what lp_live does ---
  fills 23   pooled P&L $+0.33   PEGGED 0/1   mean|inv| 1.0   mean max|inv| 5.0 (cap 20)
     15s  n=   22  markout +0.00c   net +1.45c
     30s  n=   21  markout +0.00c   net +1.48c
     60s  n=   18  markout +0.03c   net +1.58c
    per-market max|inv|: P06VCFBAR-BAR4=5

--- SKEW OFF (0.0) = no inventory lean ---
  fills 61   pooled P&L $+0.53   PEGGED 1/1   mean|inv| 15.0   mean max|inv| 20.0 (cap 20)
     15s  n=   59  markout +0.00c   net +1.00c
     30s  n=   57  markout +0.00c   net +1.00c
     60s  n=   53  markout +0.01c   net +1.01c
    per-market max|inv|: P06VCFBAR-BAR4=20*

Read: two-sided capture iff SKEW ON keeps PEGGED at 0 and mean|inv| near flat 
WHILE net/fill stays positive at 60s. Fill counts are an UPPER bound (queue 
ignored); markout is queue-independent and is the trustworthy toxicity signal.
```

## 2026-09-06 17:10 UTC — skew A/B (soccer)
```
GET /events -> 429, backing off 1.00s (attempt 1/6)
GET /events -> 429, backing off 1.00s (attempt 1/6)
GET /events -> 429, backing off 1.00s (attempt 1/6)
GET /events -> 429, backing off 1.00s (attempt 1/6)
GET /events -> 429, backing off 1.00s (attempt 1/6)
GET /events -> 429, backing off 1.00s (attempt 1/6)
SKEW A/B on 4 markets for 25 min (skew 0.01 vs 0.0), identical inputs to both arms:
   KXLALIGATOTAL-26SEP06ALAOSA-4
   KXLALIGASPREAD-26SEP06ALAOSA-ALA2
   KXSERIEATOTAL-26SEP06BFCSAS-4
   KXSERIEATOTAL-26SEP06BFCSAS-3
  20 sweeps | pegged: skew 2/4, flat 3/4
  40 sweeps | pegged: skew 2/4, flat 3/4
  60 sweeps | pegged: skew 2/4, flat 3/4
  80 sweeps | pegged: skew 2/4, flat 3/4
  100 sweeps | pegged: skew 2/4, flat 3/4
  120 sweeps | pegged: skew 2/4, flat 3/4
  140 sweeps | pegged: skew 2/4, flat 4/4
  160 sweeps | pegged: skew 2/4, flat 4/4
  180 sweeps | pegged: skew 2/4, flat 4/4
  200 sweeps | pegged: skew 2/4, flat 4/4
  220 sweeps | pegged: skew 2/4, flat 4/4
  240 sweeps | pegged: skew 2/4, flat 4/4

==============================================================================
SKEW A/B — 4 markets, 250 sweeps, identical inputs
==============================================================================

--- SKEW ON (0.01/contract) = what lp_live does ---
  fills 319   pooled P&L $+17.19   PEGGED 2/4   mean|inv| 5.2   mean max|inv| 17.8 (cap 20)
     15s  n=  304  markout -0.65c   net +3.98c
     30s  n=  298  markout -0.42c   net +4.29c
     60s  n=  291  markout +0.85c   net +5.51c
    per-market max|inv|: 6SEP06ALAOSA-4=20*, P06ALAOSA-ALA2=20*, 6SEP06BFCSAS-3=18, 6SEP06BFCSAS-4=13

--- SKEW OFF (0.0) = no inventory lean ---
  fills 491   pooled P&L $+14.94   PEGGED 4/4   mean|inv| 16.2   mean max|inv| 20.0 (cap 20)
     15s  n=  469  markout -0.61c   net +1.03c
     30s  n=  460  markout -0.53c   net +1.13c
     60s  n=  452  markout +1.04c   net +2.58c
    per-market max|inv|: 6SEP06ALAOSA-4=20*, P06ALAOSA-ALA2=20*, 6SEP06BFCSAS-4=20*, 6SEP06BFCSAS-3=20*

Read: two-sided capture iff SKEW ON keeps PEGGED at 0 and mean|inv| near flat 
WHILE net/fill stays positive at 60s. Fill counts are an UPPER bound (queue 
ignored); markout is queue-independent and is the trustworthy toxicity signal.
```
