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

**Next.** `lp_live --live --i-understand-live --pilot KXLIGAMX --prefix KXLIGAMX --minutes 60`
+ paired `ws_features --prefix KXLIGAMX`, during a live Liga MX game with Derek present
(real money, NOT autonomous). Runbook: `docs/setup/10-club-soccer-pilot.md`. Season ramp:
La Liga Aug 16 → EPL/Ligue 1 Aug 21 → Serie A Aug 22 → Bundesliga Aug 28 (all in
`ELIGIBLE_PREFIXES`, auto-screened).

**Risk rule.** Jump pick-off is warn-able from the tape (AUC ~0.8) but it's a PULL signal,
not a lean; expect a small un-dodgeable residual (`core/maker/pickoff_dynamics`).

Files here: `soccer_screen.py` (club microstructure vs WC benchmark), `breakeven.py`
(capture-vs-toxicity go/no-go curve). Engine: `core/maker/`. Data: `fct_lp_*`,
`fct_toxicity_by_family`. History: `docs/paper_pilot_findings.md`, devlog.
