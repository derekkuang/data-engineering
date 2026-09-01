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
