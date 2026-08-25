# pm_ladder_consistency — Polymarket NegRisk basket-arb screen

**Status: SCREEN BUILT + FIRST READ 2026-08-24 — leans SEALED (buy-basket), 2 thin
sell-side flags pending depth-weighted verification. READ-ONLY, $0.**

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

**Open (before any believe).** The sell-basket edge is measured via Yes-leg *bids*, which
assumes you can short at the touch; the truly executable form is buying every **No** leg
(`Σ No-ask`), a different and usually worse number. The honest close needs (a) No-leg
depth-weighted execution on the 2 flags, and (b) persistence — does either flag survive
minutes, or is it a stale-quote flicker? Both are cheap follow-ups on the same client.

**Expected verdict:** NULL, same as Kalshi — the mispricing is real at the mid but sits
inside spread + leg-count execution risk. Value is the repeatable, liveness-gated
measurement + the reusable `ingestion/polymarket.py` read client.

Files: `basket_screen.py`. Client: `ingestion/polymarket.py` (`POST /books` batch fetch —
one round trip per field, so a 128-leg field is scannable at high RTT; liveness gate
rejects dead day-of ladders that naively read as arbs).
