# soccer_mm — in-play soccer market-making

**Status: ACTIVE (the one surviving edge). Next action: live Liga MX SPREAD pilot.**

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
