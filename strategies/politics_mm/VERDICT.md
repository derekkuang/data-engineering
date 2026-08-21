# politics_mm — politics maker (compression harvesting)

**Status: GATED — paper phase CLOSED 2026-08-10; net edge resolvable only by a small
real-money pilot (deferred, Derek's call). Autonomous schedule DISABLED (workflow_dispatch
kept).**

**Thesis.** Documented politics compression — favorites underpriced (calibration slope
1.25–1.33, ECE 0.026 vs crypto's 0.007) — is UNtradeable as a taker (trapped in spread+fee)
but GROSS-POSITIVE as a maker: MID +2–3.6%/contract, MAKER@bid **+3.4–7.3%/contract**,
event-block CIs entirely >0 (`politics_calibration.py`). The FIRST gross-positive of the
entire alpha hunt.

**Paper result (7 sessions, ~640 fills, `core/maker/lp_paper_pilot --category Politics…`).**
Short-horizon markout leans **NON-FATAL**: 6 sessions benign ~0c, 1 toxic −5.5c
(Brazil-Senate news shock). The fast-news toxicity that killed soccer-comparable gross
spreads is largely ABSENT here.

**Why paper can't finish it.** (1) Politics is too thin — the picker finds 0–1 makeable
markets/session (`--markets 10` → 1 measured, repeatedly), so every read is a single
one-sided market. (2) The two bigger killers — real **fill-rate** and **months-long
directional inventory** — only exist with real capital. A discovery rewrite (volume-ranked
`/markets` scan) would firm the distribution but can't touch the capital-gated killers.

**Character.** A slow directional-inventory game, NOT the fast sports-MM survivor.

Files here: `politics_calibration.py`. Findings: `docs/paper_pilot_findings_politics.md`
(verdict on top). Lit: `docs/research/prediction_market_literature.md` (politics = worst
calibrated domain — consistent with our nulls elsewhere).
