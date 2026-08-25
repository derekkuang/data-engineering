# pm_ladder_consistency — Polymarket NegRisk basket-arb screen

**Status: CLOSED 2026-08-24 — SEALED (null), the fee-free twin of the Kalshi
threshold-arb null. READ-ONLY, $0.**

**Thesis.** On a mutually-exclusive-and-exhaustive (MECE) NegRisk field the Yes legs must
sum to $1. Fee-free Polymarket *might* leave executable complement arbs that Kalshi's
taker fee traps (the fee-free twin of our Kalshi `strategies/btc_direction/threshold_arb`
null). Buy every Yes leg for `Σask < 1` → bank `1 − Σask` risk-free; sell every leg for
`Σbid > 1` → bank `Σbid − 1`.

**First read (`basket_screen.py`, 24 liquid fields, snapshot).**
- **Median Σask 1.020, median Σbid 0.9805.** Buying a full basket costs ~2c over par;
  selling pays ~2c under. **Zero buy-basket arbs** over the 0.5c threshold. The clean
  risk-free direction is sealed — bots hold the complement, exactly the Kalshi
  threshold-arb result on a fee-free venue.
- **2 sell-side flags** — `pro-football-2027-champion` (32 outcomes, +2.3c) and
  `blast-open-porto` (16 outcomes, +1.3c) — but both are **thin, high-leg futures/esports
  fields with min bid depth ~70–100 shares**. Almost certainly snapshot artifacts: the
  +2c vanishes when you execute 16–32 separate sell legs into ~$100-deep books against
  bots. This is the "median honest, tails thin+illiquid" pattern seen on every competed
  axis in the repo.

**Executable close (`--verify`, No-leg check).** A Yes-bid `Σbid>1` flag is not a real
trade — you can't naked-short a Yes leg; the executable form is BUYING every No leg
(`verify_sell_basket`: arb iff `Σ No-ask < N−1`, the exact twin of `Σ Yes-bid > 1`). Two
findings killed the flags: (1) **non-persistence** — the sell flags drifted between two
snapshots minutes apart (2 → 1; the +1.3c `blast-open-porto` flag evaporated), the
signature of stale-quote flicker, not a standing arb; (2) **not executable** — the one
persistent flag (`pro-football-2027-champion`) has a **one-sided No leg**, so the basket
literally can't be bought. No buy-basket arb ever appeared.

**VERDICT: SEALED (null).** Fee-free Polymarket prices its MECE complements as tightly as
fee-bound Kalshi prices its threshold ladders — bots hold the seam; the residual
inversions sit inside the spread / are un-executable. Same wall as everything competed.
Value delivered: a repeatable, liveness-gated, executable-verified measurement + the
reusable `ingestion/polymarket.py` client that unlocks P2 (cross-venue basis) and P3
(calibration).

Files: `basket_screen.py` (`--verify` = No-leg executable check). Client:
`ingestion/polymarket.py` (`POST /books` batch fetch — one round trip per field, so a
128-leg field is scannable at high RTT; liveness gate rejects dead day-of ladders).
