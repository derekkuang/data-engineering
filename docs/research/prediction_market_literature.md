# Prediction markets (Polymarket & Kalshi): the empirical literature

*Side-track research, 2026-08-05. Produced by a deep-research harness (20 sources — mostly 2025–2026
working papers; 92 claims extracted; 25 put through 3-vote adversarial verification). The final
synthesis step hit a session limit, so this write-up is hand-synthesized from the verified set:*
***13 claims survived 3-skeptic verification (high confidence); 4 were killed; 8 more are from the same
primary sources but their verification votes errored out (medium confidence — flagged inline).***

**Headline:** these are genuinely good forecasters — well-calibrated on average, beating polls,
bookmakers, and weather models, and *leading* other markets in price discovery — with one robust catch:
**calibration is highly domain-specific, and politics is the weak spot where a real, large mispricing lives.**

---

## Q1 — Calibration: yes, but domain-specific

**Well-calibrated on average [verified 3-0]:**
- Polymarket prices "closely track realized probabilities and slightly outperform bookmaker odds" across **478M trades** ([SSRN 5910522](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5910522)).
- Kalshi win-fractions "fluctuate around the 45-degree line" — "pretty good predictors" ([Whelan et al. / CEPR dp20631](https://www.karlwhelan.com/Papers/Kalshi.pdf)).
- Flagship study — **353M trades / 429K contracts** across both venues — confirms calibration varies systematically by domain, time-to-resolution, and trade size ([arXiv 2602.19520](https://arxiv.org/abs/2602.19520)).
- Practitioner benchmark (540K resolved markets): both venues **Brier ≈ 0.09**, beating weather models (0.14–0.15), sportsbooks (0.18–0.22), polls (0.27–0.31) ([Keyrock](https://keyrock.com/knowledge-hub/prediction-market-accuracy-brier-scores/)) [context].

**Domain split [medium — same verified study, per-domain votes errored]:** Politics is *by far* the worst
(**ECE 0.117**); Crypto (**0.007**), Sports (0.008), Weather (0.016), Finance (0.016), Entertainment (0.022)
are near-perfectly calibrated. Accuracy also improves with liquidity and toward resolution.

## Q2 — Longshot bias: two different stories

- **Kalshi — classic favorite-longshot bias in RETURNS [verified 3-0]:** contracts < 10c **lose >60%**; > 50c
  earn small positive returns (significant > 70c); average pre-fee return **≈ −20%** (Whelan/CEPR, 313,972
  prices, 2021–Apr 2025). A Mincer-Zarnowitz test **rejects unbiasedness**. Longshots systematically overpriced.
- **Politics (both venues) — the OPPOSITE [verified 3-0]:** "persistent underconfidence… prices compress
  toward 50%" (favorites *underpriced*), replicates on Polymarket (arXiv 2602.19520). A **reverse** longshot bias.
- **Killed:** "Polymarket has *no* longshot bias" (0-3) *and* "Polymarket has the *classic* longshot bias" (1-2).
  Polymarket's bias is **category-dependent and does not cleanly match the horse-racing pattern.**
- Reconciliation: the Kalshi "−20%/longshots lose 60%" is a money-return result across all categories; the
  "compression toward 50%" is a calibration-slope result specific to politics. Both are real; different things.

## Q3 — Pre-resolution drift: real convergence, not a free lunch

- **Convergence toward the outcome [verified 3-0]:** Kalshi MAE "declines with each day… smooth until the
  last day, then a steep drop" (Whelan/CEPR). Accuracy rises as resolution nears.
- **The *exploitable* version was killed:** a resolution-pressure mechanism (MMs absorbing flow at unfavorable
  prices near 0/1, negative Kyle's lambda) was contested/refuted (1-2, [arXiv 2606.04217](https://arxiv.org/pdf/2606.04217)).
  Convergence is the market correctly incorporating info over time — **not a documented, fee-surviving edge.**
- **Cross-venue inefficiency IS documented [context]:** Polymarket vs Kalshi violate the Law of One Price —
  execution-adjusted spreads ~$0.03 (up to $0.07) for the "same" event, because contracts aren't truly fungible
  (different resolution sources — "semantic non-fungibility," [arXiv 2601.01706](https://arxiv.org/abs/2601.01706)).
  Within-Polymarket arbitrage half-lives collapsed from *hours to ~0.74 min* by the 2024 election
  ([arXiv 2512.16030](https://arxiv.org/pdf/2512.16030)) — efficiency scales with attention.

## Q4 — News incorporation: markets *lead* (the strongest finding)

- **Lead financial markets [verified 3-0]:** 2024 election night, Polymarket reached 90% of its overnight move
  **~3.75 h before** E-mini S&P futures, and led at every stage (25%/50% of the move 15/25 min ahead)
  ([Bogazici price-discovery study](https://web.bogazici.edu.tr/torul/pridis.pdf)).
- **Lead sportsbooks/dealers [verified 3-0]:** Jan–Nov 2024, order-book exchanges (Polymarket 47.5%, Betfair
  37.1%) held **~85% of price discovery**; no dealer sportsbook exceeded 11% — even sharp-book Pinnacle was a
  price-*follower*. Kalshi reached an information-leadership share of 1.69 **within weeks of launch** [2-1].
- **Can lead the news itself [verified 3-0]:** Polymarket's Biden-withdrawal market hit **0.70 one minute
  *before* the public announcement** ([arXiv 2603.03152](https://arxiv.org/html/2603.03152)).
- **But not uniformly [verified 2-0]:** the *same* market processed shocks differently — the Biden-Trump debate
  move **reversed within 3 h** (overreaction), while the Trump assassination-attempt repricing **persisted and
  doubled** by 3 h. Fast ≠ always right on impact.
- **vs polls [medium]:** markets moved sharply on 2024 events while aggregate polling barely budged
  ([arXiv 2507.08921](https://arxiv.org/html/2507.08921)).

## Cross-validation with this project's own measurements

Our measured results are textbook instances of the literature, not method artifacts:
- Our Kalshi 15-min BTC calibration (**ECE ~0.5%**, favorite-longshot **null**) ≈ the flagship study's **crypto**
  domain (**ECE 0.007**). "Efficiently priced, no bias" is *what the crypto category does*.
- Our settlement-lag null (no minute-level edge) ≈ "convergence is real but not an exploitable free lunch."
- Our closed cross-venue arb (basis-risk) ≈ the Law-of-One-Price "semantic non-fungibility" result.

## Leads for this project (opinion, 2026-08-05)

1. **POLITICS calibration is the one lead where the mispricing (ECE 11.7%) is an order of magnitude larger than
   the trading friction** — the opposite of every dead end so far (crypto FLB was real but *trapped inside the
   1c spread*). Whether the compression-toward-50% survives the spread + fees + a months-long hold + sharp
   competition is UNKNOWN and cheaply measurable on our own data (reuse the `favorite_longshot.py` /
   `altcoin_efficiency.py` market-internal pattern — price + outcome, no new ingestion — on resolved Kalshi
   politics). Caveats: it's a DIRECTIONAL/hold strategy (not the MM survivor), slow-resolving (capital tied for
   months), sharp-competed, and the "compression" is a working-paper result that could be a selection artifact.
2. **Reproduce the domain-calibration map on our own universe** — a pure-DE deliverable (calibration by category
   across all Kalshi series) valuable regardless of edge, and it independently validates/refutes the 353M-trade
   study on our data + shows WHERE any mispricing lives.
3. **Confirmed dead (the research corroborates our own nulls):** cross-venue LoOP spreads (= basis risk),
   lead-lag exploitation (= a latency race we lose), pre-resolution drift (= convergence, not tradeable).

## Sources
[arXiv 2602.19520](https://arxiv.org/abs/2602.19520) (353M-trade calibration) · [Whelan/CEPR](https://www.karlwhelan.com/Papers/Kalshi.pdf) (Kalshi FLB + convergence) · [SSRN 5910522](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5910522) (Polymarket calibration) · [Bogazici price-discovery](https://web.bogazici.edu.tr/torul/pridis.pdf) · [arXiv 2603.03152](https://arxiv.org/html/2603.03152) (news events) · [arXiv 2601.01706](https://arxiv.org/abs/2601.01706) (Law of One Price) · [SSRN 6670638](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6670638) (Polymarket Brier/liquidity) · [repec d5yx2](https://ideas.repec.org/p/osf/socarx/d5yx2_v1.html) (2024 venue accuracy) · [arXiv 2606.04217](https://arxiv.org/pdf/2606.04217) (resolution pressure) · [arXiv 2507.08921](https://arxiv.org/html/2507.08921) (markets vs polls) · [Keyrock](https://keyrock.com/knowledge-hub/prediction-market-accuracy-brier-scores/) (Brier benchmark)
