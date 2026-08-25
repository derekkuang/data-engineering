# Polymarket structural-edge landscape (deep-research, 2026-08-24)

Multi-source, adversarially-verified research pass (23/25 claims confirmed 3-0 or 2-1)
run to decide whether a Polymarket branch is worth building. **Conclusion: the branch is
closed by evidence — all three legs (structural edge, execution access, fresh anomaly)
fail.** This corroborates the live measurement in
`strategies/pm_ladder_consistency/VERDICT.md`.

## 1. NegRisk basket/complement arbs — real, documented, bot-saturated

- NegRisk (mutually-exclusive-and-exhaustive) markets are THE documented source of
  complement/basket arbitrage. Common, not rare: **662 of 1,578 NegRisk markets** had ≥1
  arb opportunity over Apr-2024→Apr-2025 (IMDEA, AFT 2025, arXiv 2508.03474).
- But **actively bot-harvested to saturation**: ~$40M total extracted in that window; the
  single largest arbitrageur took $2.0M across 4,049 tx; "buying NO" was the top strategy
  ($17.3M, Polymarket publicly acknowledged it). Converter-arb profit is bot-concentrated
  (top-10 addresses = 75% of $1.086M — TU Munich, arXiv 2608.00666).
- **Profitability has collapsed**: median converter profit fell ~1 USDC (mid-2024) →
  ~0.20 → **~0.08 USDC/conversion (early 2026)**; violation windows median **~16s**;
  sub-100ms bots capture ~73%; arb windows shrank 12.3s → ~2.7s.
- Asymmetry worth noting: exploitable violations are almost entirely **YES-side**
  (2,098 vs 36 NO-side) because the NegRisk-Adapter conversion path is one-directional.

**This is exactly our P1 live finding.** Our screen measured median Σask 1.020 / Σbid
0.980 (buy-basket sealed, sell-flags non-persistent and un-executable via a one-sided No
leg). The literature explains *why*: a mature sub-second bot layer holds the seam, and the
No-side path is structurally blocked. Our SEALED verdict is the correct 2026 state.

## 2. Fees — the zero-fee premise is STALE (kills the branch's core thesis)

The entire reason to branch to Polymarket was "fee-free venue leaves capturable versions
of the mispricings Kalshi's taker fee traps." **That premise is dead as of 2026:**
Polymarket introduced **category-based taker fees (~$1.00–1.75 per 100 shares = ~1–1.75c),
maker $0, only geopolitical markets fee-free** (Polymarket docs; IMDEA notes the old
zero-fee assumption applied only through the 2025 study). That's the same order as Kalshi's
~1.75c taker fee — the one structural advantage evaporated except in a narrow geopolitical
slice. A live maker-rewards program exists (daily UTC payout, $1 min, quadratic-in-spread
scoring) — but we already measured Polymarket maker markout at −0.50c (worse than Kalshi)
and closed that in `strategies/polymarket/`.

## 3. Polymarket US / QCX — regulated, but NO documented API (execution door shut)

- QCX LLC d/b/a **Polymarket US** is a live CFTC-registered **Designated Contract Market**
  (Amended Order of Designation Nov 25, 2025; launched Dec 3, 2025; expanding into macro/CPI
  contracts, listing no earlier than Apr 9, 2026). Primary sources: CFTC filings + PRNewswire.
- It uses an **intermediated FCM/brokerage model**, distinct from the offshore on-chain
  (Polygon/USDC) platform.
- **Critical gap:** none of the primary sources document a **public API** for the onshore
  DCM. So the venue a US person can legally use when home (a) is app/brokerage-intermediated
  and (b) has no confirmed programmatic access — a systematic bot has no documented door.
  (Offshore remains close-only for US persons regardless of IP — see the geoblock finding.)

## 4. Anomalies — only politics underconfidence, which just re-confirms our Kalshi edge

- Prices are broadly **well-calibrated** (mean abs calibration error ~2.1pp); mispricing is
  **transient** (early-life + near-resolution, ~80-min post-news repricing lag).
- **General favorite-longshot bias was REFUTED (0-3) as a Polymarket edge** — the behavioral
  tendency is Yes/default overtrading, not a tradeable FLB.
- The one robust anomaly is **political-market underconfidence** (compression toward 50%,
  favorites underpriced): Polymarket Politics logistic slope **~1.45** vs Sports/Crypto ~1.06
  (Le, arXiv 2602.19520). This **replicates our Kalshi politics-compression finding** (slope
  1.25–1.33, `strategies/politics_mm/`) — it's not a NEW edge, it's the SAME edge we already
  have and which is gated on capital, not discovery.

## Strategic verdict

Polymarket fails on all three legs simultaneously: (1) its structural edge (NegRisk basket
arb) is bot-saturated to ~0.08 USDC and now fee-bound; (2) its one fresh-looking anomaly
(politics) merely re-confirms an edge we already hold on Kalshi; (3) the execution door is
shut — offshore is close-only for US persons, onshore QCX has no documented API. **Recommend:
keep Polymarket as a free data/calibration venue only; the tradeable edge stays
Kalshi-retail-specific.** Revisit trigger: QCX ships a public trading API AND lists market
types with a measurable edge.

## Open question the research could not close
- Does QCX expose programmatic API trading (protocol/auth)? CFTC filings are silent. This is
  the single fact that would reopen an execution path; worth a direct check if Polymarket US
  publishes developer docs.

_Sources: arXiv 2508.03474 (IMDEA AFT 2025), arXiv 2608.00666 (TU Munich), arXiv 2605.00864
(UCLA NBA arb), arXiv 2602.19520 (Le, calibration), SSRN 5910522 (Reichenbach & Walther),
CFTC filings rules03262642008 / ptc02092638974, Polymarket liquidity-rewards docs._
