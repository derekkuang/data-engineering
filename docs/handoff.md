# Handoff — crypto-DE / Kalshi platform (as of 2026-08-07)

Read this first in a new chat, then `docs/devlog.md` (newest-first) for full detail. The project's
persistent memory (`~/.claude/.../memory/MEMORY.md`) has the same state in compact form.

## What this project is
A **"full Kalshi platform"**: a data-engineering warehouse (S3 → Glue `crypto_raw` → Athena → dbt, 16
models) that screens the whole Kalshi exchange daily, auto-captures in-play microstructure/toxicity data,
runs a statistical edge-verdict, and trades the one thin surviving edge. **The trading bot IS the DE
project; durable value = the platform + the honest "efficient to the limits of arbitrage" arc**, edge or not.

## Git / infra state
- `origin/main` pushed through **`64fadda`**; local is ahead by the 08-07 politics-maker commit **`462b0a0`**
  + this session's paper-maker/handoff commit (see `git log origin/main..HEAD`). **Push to activate the
  scheduled workflows.** After weekends, the paper-pilot bots commit findings → `git pull` before working.
- **GitHub Actions:** `ci.yml` (pytest + dbt build vs Athena on push), `pipeline.yml` (daily 02:30 UTC
  ingest + build), `ws-capture.yml` (in-play capture — 00/02 UTC now **soccer-only**, 19/23 UTC `--wide`
  controls), `paper-pilot.yml` (weekend zero-money **soccer** paper-maker), `paper-pilot-politics.yml`
  (NEW: daily zero-money **politics** paper-maker). Watch Actions minutes (private repo, likely > free tier).
- AWS: profiles `crypto-de-pipeline` (read-only) + `crypto-de-deployer` (admin/DDL); bucket
  `derekkuang-crypto-de-raw-546712138633-us-east-1-an`; workgroup `crypto_wg`; dbt `DBT_PROFILES_DIR=dbt`.

## The alpha map — everything is closed EXCEPT one live lead
- **Closed (all measured-null/uncapturable, detail in devlog + topic memory):** ~15-axis BTC direction hunt,
  weather, tennis, perps, cross-venue arb, Polymarket, goal-taking/latency, MVE parlays (bias ~1.4× real but
  structurally uncapturable — buy-only book), and the 07-29 edge-scan trio (breadth axis built-but-null,
  esports downgraded, PM-vs-sportsbook backtested null). External lit review corroborates our nulls
  (`docs/research/prediction_market_literature.md`: calibration is domain-specific — crypto/sports efficient
  like our results; markets LEAD news/futures/sportsbooks in price discovery).
- **THE ONE THIN REAL EDGE = Kalshi in-play SOCCER market-making** (WC/SPREAD: +0.59c spread capture − 0.135c
  toxicity). Whether it TRANSFERS from World Cup to year-round club soccer is the open question, resolvable
  ONLY by real fills. Pick-off risk rule (`ml/research/pickoff_dynamics.py`): jumps are warn-able via the
  TAPE (trade/flow/vol AUC ~0.8) but a PULL signal not a lean → monitor tape, pull on a surge.
- **THE ONE OPEN NEW LEAD (08-07, first gross-positive of the whole hunt) = POLITICS MAKER.**
  `ml/research/politics_calibration.py` (3,968-resolved-market calibration + 3-regime backtest): politics
  prices are compressed toward 50% (favorites underpriced, slope ~1.25-1.33, ECE 0.026 vs crypto 0.007).
  UNtradeable as a taker (trapped in spread+fee), but **GROSS-positive as a maker** — MID +2-3.6%/ct,
  MAKER@bid +3.4-7.3%/ct, event-block CIs entirely > 0. **Gross of the real killers: fill-rate/adverse-
  selection, news-toxicity, months of directional inventory. NET is the open question.**

## Active / next actions
1. **Politics-maker NET test (IN PROGRESS):** `lp_paper_pilot --category Politics,Elections,World,Economics
   --markets 10` paper-makes the 10 most-active political favorites AT ONCE (zero-capital, simulated fills)
   to measure the **markout** (news-toxicity) that the gross number ignores. **Multi-market mode now built**
   (08-07): pools fills across markets → ~10× faster markout accumulation (a live 30s probe pooled 283 fills
   vs 96/12min single-market); each fill still marked vs its own market's mid; unit-tested
   (`tests/test_lp_paper_pilot.py`). Scheduled daily via `paper-pilot-politics.yml` (`--markets 10 --poll 6`,
   20:00 UTC) → accumulates to `docs/paper_pilot_findings_politics.md`. **Next chat: read that file's trend**
   (needs a few days of the multi-market runs). If markout is strongly negative → toxicity eats the edge (as
   it did soccer); if benign → the gross maker edge may survive.
2. **THE LIVE MEX (Liga MX) SPREAD pilot** — the real-money experiment that confirms the soccer edge transfer.
   BLOCKED on a live Liga MX game + Derek present (NOT autonomous — real money). `lp_live --live
   --i-understand-live --pilot KXLIGAMX --prefix KXLIGAMX --minutes 60` + paired `ws_features --prefix
   KXLIGAMX`; runbook `docs/setup/10-club-soccer-pilot.md`. Acct ~$8; `--test-order` pre-flight parked for
   Derek; bot is fail-CLOSED (idles until a family is CONFIRMED). Soccer season ramps mid-Aug (La Liga 8/16 …).
3. **Soccer paper-pilot** runs autonomously each weekend → `docs/paper_pilot_findings.md` (de-risks the pilot).

## Derek's manual items
- Dashboard UI smoke-run (`uv sync --group dashboard && uv run --group dashboard streamlit run dashboard/app.py`).
- README voice pass (`96f3784`, discloses real-money +$70/9d — his call if repo goes public).
- AWS hygiene: disable the dead BTC Lambda (`aws events disable-rule --profile admin --name
  kxbtc-orderbook-decision-minutes`) + add a ~$10/mo budget alert. Confirm repo visibility vs Actions minutes.
- Deferred permissioned (need a shared-mart DROP): convert `fct_toxicity_by_family` to Iceberg incremental;
  drop orphaned `crypto_marts.fct_ws_markout__dbt_tmp`.
