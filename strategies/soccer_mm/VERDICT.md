# soccer_mm — in-play soccer market-making

**Status: CLOSED for now (2026-10-01) — null-leaning on club soccer, NOT disproven; the WC
result stands. Revisit triggers in the 2026-10-01 section at the bottom.**

**Thesis.** Kalshi's in-play soccer TOTAL/SPREAD books carry wide retail spreads, and
soccer's rare discrete scoring keeps the book mean-reverting — a maker captures spread
faster than adverse selection taxes it.

**Proven (real money, World Cup).** Net **+$53 over 5 days**; spread capture **+0.59c/fill**
is the real edge; realized WC/SPREAD markout **−0.135c/fill** = a mild but REAL
adverse-selection tax (15,829 fills, ET-day-block bootstrap — `core/maker/realized_toxicity`).
Soccer TOTAL reads jump-benign where tennis/MLB read jump-toxic (`core/maker/edge_verdict`).

**Open question.** Does WC capture-efficiency + toxicity TRANSFER to year-round club soccer?
`breakeven.py`: club SPREAD breaks even at ~1c spread — width is NOT the constraint; only
real fills resolve it. `soccer_screen.py`: club near-money spreads are in-band (Liga MX
widest ~4c).

**POOLED CLUB-SOCCER VERDICT (2026-09-02, `edge_verdict --pool-club-soccer`).** Waiting 8
match-days *per league* is ~2 months (a league plays ~1–2 days/wk). The captured data shows
the big-five + UCL/UEL + minor European leagues share ONE SPREAD/TOTAL toxicity profile
(per-league jump 0.02–0.17c, all sub-0.25), so `edge_verdict` now pools them into a
`CLUB_SOCCER` family (re-aggregated per market_type×capture_day, same day-block bootstrap).
Pooling reaches the floor via the leagues' DIFFERENT kickoff schedules. Read:
**`CLUB_SOCCER/SPREAD` — 9 days (floor cleared), jump 0.093 = BENIGN** (the goal-pick-off
axis that kills makers reads clean, like WC); **`CLUB_SOCCER/TOTAL` — 18 days**, both axes
trending benign (flow −0.01 [−0.26,+0.20], jump 0.229 point-benign). Neither is CANDIDATE
yet: the flow-axis CI is still too wide (needs more SPREAD obs to sit under the 0.10 bound)
AND there are zero real fills (pooled realized capture = None → caps at CANDIDATE regardless).
So the fail-CLOSED bot still won't auto-quote — correct. The pilot is what resolves both.

**Next.** A live big-five SPREAD/TOTAL pilot (La Liga best-captured): `lp_live --live
--i-understand-live --pilot KXLALIGA --prefix KXLALIGA --minutes 60` + paired `ws_features
--prefix KXLALIGA`, during a live match with Derek present (real money, NOT autonomous) —
its fills give the realized capture + tighten the flow CI toward CONFIRMED. Runbook:
`docs/setup/10-club-soccer-pilot.md`. All leagues in `ELIGIBLE_PREFIXES`, auto-screened.

**Risk rule.** Jump pick-off is warn-able from the tape (AUC ~0.8) but it's a PULL signal,
not a lean; expect a small un-dodgeable residual (`core/maker/pickoff_dynamics`).

Files here: `soccer_screen.py` (club microstructure vs WC benchmark), `breakeven.py`
(capture-vs-toxicity go/no-go curve). Engine: `core/maker/`. Data: `fct_lp_*`,
`fct_toxicity_by_family`. History: `docs/paper_pilot_findings.md`, devlog.

## 2026-09-12/13 — FIRST REAL MONEY + the constraint moved: it is OPPORTUNITY, not toxicity

**First live club-soccer pilot (real money, hard-capped).** 29 fills over 12 market-sessions,
**−$0.22** (balance $15.61 → $15.39). Mechanically a success: **23 of 29 fills were PASSIVE
maker fills at zero fee** (soccer is maker-fee-free), inventory returned to flat every time,
nothing pegged, no kill switch tripped, clean exits, zero open positions at the end. The
6 taker fills ($0.035) were the aggressive flatten — risk machinery working, not malfunction.

**Finding 1 — the losses were NOT adverse selection.** The largest single sample
(`KXEPLTOTAL-…TOTEVE-1`, 18 fills) had markout **+0.139c — FAVORABLE** — and still realized
−$0.084. The mid moved our way after fills and we lost anyway. The money went to **spread +
the cost of crossing to flatten**, not to pick-off. That is a different failure mode than the
one our entire toxicity apparatus (two axes, day-block bootstraps, jump detection) measures.

**Finding 2 — real fill rate is 12–50x below paper.** Live: 0.16 fills/min (session 1),
0.57 (session 2). Paper: 7–31 fills/min. **8 of 12 market-sessions got ZERO fills.** Paper
assumes any print at your price fills you; real queue position says otherwise. This is the
number paper structurally cannot produce, and the reason to have run the pilot at all.

**Finding 3 (the screen) — a club book is makeable only ~6% of the time.** Across 253k
captured snapshots, a club TOTAL/SPREAD book is simultaneously wide enough (≥2c) AND active
(≥1 trade/min) just **5.9% (TOTAL) / 6.8% (SPREAD)** of the time — median spread **1.31c /
1.83c**. Half the time the spread is under 2c; ~84% of the time there is no flow. A 6% duty
cycle explains the fill rate with no toxicity story required.

**Finding 4 — the league ranking INVERTS, and the marquee competitions are worst:**

| league | median spread | % makeable (2c,1tr) | % rich (3c,2tr) |
|---|---|---|---|
| **LigaMX** | 2.03c | **12.1** | **8.6** |
| MLS | 1.00c | 9.6 | 5.6 |
| **Brasileirao** | **3.71c** | 8.8 | 5.9 |
| Ligue1 | **5.11c** | 7.9 | 6.1 |
| SerieA | 3.56c | 6.8 | 4.9 |
| EPL | 1.85c | 6.4 | 3.1 |
| LaLiga | 1.00c | 5.7 | 3.5 |
| UCL | 1.00c | **4.5** | 3.1 |

Americas leagues are ~2x more makeable than the big five. UCL and La Liga sit at a **1.00c
median** — pinned to the minimum tick. More prestige and volume ⇒ more competing makers ⇒ the
spread is competed away. Same mechanism that killed Polymarket for us.

**This corrects the 2026-09-02 pilot-target call.** We switched the pin from Liga MX → La Liga
because big-five was better-CAPTURED and in-season. That measured our own logging, not
tradeability: on makeable duty cycle **La Liga is near the bottom and Liga MX is the best**.

**REVISED TARGETS: Liga MX / Brasileirao / Ligue 1** (real spread, best duty cycle, and the
Americas pair is year-round). NOT the big five.

**The strategy is not disproven — but the binding constraint moved.** Flow toxicity reads
benign-to-favorable; what is scarce is *makeable opportunity*. Two implications:
1. **Multi-market LIVE quoting is now the highest-value capability.** At a 6% per-book duty
   cycle, quoting ONE market at a time is structurally starved. `lp_pilot` has multi-market
   (paper); `lp_live` does not.
2. **Economics stay small.** Even at WC's +0.45c/fill and WC's ~2.2 fills/min, size 1 is
   ~$0.59/hour. This is a thin, real edge — a portfolio artifact and a discipline exercise,
   not income.

**STOPPING RULE (pre-committed, to avoid indefinite grinding):** run at most **5 more live
sessions** on Liga MX / Brasileirao / Ligue 1 with multi-market enabled. If cumulative
realized capture is not positive by then, close the track and write it up as a null —
the honest arc is the deliverable either way.

## 2026-10-01 — CLOSED for now: the jump axis drifted toxic and real capture is not positive

**Second live session (2026-09-14, multi-market `lp_live`, Serie A / EPL / La Liga).** 66
fills over 7 markets, **net +$0.85** — but **+$1.01 of it is ONE goal**: on
`KXSERIEATOTAL-26SEP14INTUDI-8` we were long 2 at ~20c when a goal sent the Over to ~90c.
The same market shows the risk side too: our skewed ask at 28c was still resting when the mid
hit 79.5c and got lifted (~51c pick-off on one contract). Ex that market: **−$0.16 on 58
fills**. The session is also contaminated by the thread-collision bug (two threads on one
ticker cancelling each other's orders; fixed `9e1722c`), which duplicated rows in
`data/lp_sessions.csv` — the numbers here take the LAST row per ticker (`kalshi_gross` is
Kalshi's own per-ticker realized P&L, gross of fees; net = gross − fees, which reproduces
the 09-12 −$0.22 exactly). It ran on the big five, not the revised targets.

| real money, club soccer | fills | net | ex-jump |
|---|---|---|---|
| 2026-09-12 | 28 | −$0.22 | −$0.22 |
| 2026-09-14 | 66 | +$0.85 | −$0.16 |
| **total** | **94** | **+$0.63** | **−$0.38 (−0.44c/fill)** |

**Skew A/B replicated — 19/19 runs, 13 days.** Skew ON beat OFF on net/fill @60s in every
run. Fill-weighted, day-block bootstrap: ON **+3.83c [+2.91, +4.39]**, ON−OFF **+2.11c
[+1.68, +2.52]**, split-half stable (ON 4.21 / 3.59). Markout @60s ON −0.55c [−1.24, −0.08],
~4x the WC's −0.135c. Skew is a real improvement (and `lp_live` already has it), but this is
a RELATIVE result inside the simulator: paper's absolute level (+3.8c/fill) vs real
(≈ −0.4c/fill ex-jump) is the 12–50x queue gap from 09-12 again plus the flatten cost paper
never charges.

**The pooled verdict drifted toward toxic with 3–4x more days** (`edge_verdict
--pool-club-soccer`, re-run 2026-10-01):

| family | 2026-09-02 | 2026-10-01 |
|---|---|---|
| CLUB_SOCCER/SPREAD | 9 days, jump 0.093 = BENIGN | 31 days, jump 0.269, INCONCLUSIVE |
| CLUB_SOCCER/TOTAL | 18 days, jump 0.229 | 42 days, jump 0.350, INCONCLUSIVE |

Per league, **zero** club SPREAD/TOTAL families read jump-benign. Jump-TOXIC (CI above
0.25c): LaLiga TOTAL + SPREAD, Brasileirao TOTAL + SPREAD, LigaMX TOTAL, SerieA TOTAL,
Bundesliga TOTAL; LIGAMX/SPREAD is also flow-TOXIC (+0.38c [+0.21, +0.56]). The early
per-league benign reads (LaLiga/SPREAD 0.115, SerieA/SPREAD 0.011 on 09-02) were small-sample.

**Why closed.**
1. The load-bearing assumption of the transfer thesis — club soccer is jump-benign like the
   WC — did not survive more data.
2. Opportunity and toxicity point in opposite directions: the makeable leagues (Liga MX,
   Brasileirao: wide + flowing) are wide BECAUSE goals pick makers off; the calm big-five
   books are pinned at the 1c tick. The spread is the price of the risk, not a free lunch —
   the same mechanism that closed Polymarket.
3. The fail-CLOSED gate itself would now refuse every revised target; more live sessions
   would mean `--pilot`-overriding the gate on exactly the risk it exists to catch.
4. The pre-committed stopping rule caps live sessions at 5; it does not require spending
   them. And it has ~no power anyway: one goal swings ±$1 against ~±$0.20/session of capture.

**PROVEN vs ASSUMED.** Proven: the jump-axis drift (31–42 capture-days, day-block CIs); the
paper-vs-real gap; skew's relative benefit. NOT proven: that realized club capture is
negative — 94 fills over 2 sessions cannot establish that. Hence "closed for now", not
"disproven".

**Revisit triggers.** (a) a sufficient-sample `edge_verdict` read with a club SPREAD/TOTAL
family jump-BENIGN on its CI (ws-capture keeps running, so this is monitored for free);
(b) the next major international tournament (the WC conditions are where the edge was real);
(c) a pull-on-tape-surge rule actually wired into `lp_live` — `pickoff_dynamics` documents it
but the live bot does not implement it, and the INTUDI-8 stale ask is the case it targets.

**Operational state at close.** `skew-ab.yml` + `paper-pilot.yml` disabled via
`gh workflow disable` (re-enable: `gh workflow enable <file>`); `ws-capture.yml`,
`pipeline.yml`, `ci.yml` stay on. EC2 runner (`docs/setup/11-ec2-runner.md`) was never
provisioned. Kalshi account $0.0061, no positions, no fills since 09-15. The two live
sessions never reached the warehouse (`realized_$` is empty for CLUB_SOCCER).
