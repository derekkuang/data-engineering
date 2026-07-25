# 10 — Club-soccer maker PILOT runbook (Liga MX)

The one open question after Step 0/Step 1: does the WC soccer maker edge **transfer** to year-round
club soccer? Desk analysis can't answer it — capture-EFFICIENCY (do we bank the spread as well as
WC/SPREAD did?) and goal-jump TOXICITY are only observable from **real fills**. This pilot is the
cheapest experiment that resolves it: a tiny, hard-capped live maker on **Liga MX** (the best live
candidate — 2c in-play spread + high volume) run **with paired `ws_features` capture of the same
book**, so one live game yields (a) realized capture-efficiency, (b) goal-jump toxicity from the
fill-anchored markout, and (c) the first **overlap** of real fills + captured microstructure at the
same market-time — the instrument validation (roadmap Step 7) that has had no overlap (fills=June,
capture=July).

## Safety model (why this is a small bet)

- **Fail-CLOSED gate.** The bot quotes NOTHING by default (nothing is CONFIRMED yet). `--pilot
  KXLIGAMX` is the ONE audited override that lets Liga MX through the family gate. It does NOT bypass
  the other guards: toxic-type exclusion (ITF), the recent-trade floor (≥15), mean-reverting-only
  (TOTAL/SPREAD), and the mid band. Verified end-to-end offline (`quotable.py` / `lp_gate`).
- **Pilot hard caps** (`--pilot`): quote size forced to **1**, session kill **-$5**. Plus the
  standing per-market kill and the ±MAX_POSITION inventory cap.
- **Account is the last backstop:** balance was ~$8 at last check — a natural ceiling on total loss.
- Sessions log under `CONFIG_VERSION = 2026-07-25-gated`, size 1 — cleanly separable from the WC runs.

## Preconditions (all must hold)

1. **A Liga MX game is IN PLAY** (not pre-game). Check: `python scratchpad/mex_check.py`-style — the
   real signal is **nonzero recent Liga MX trade flow** + near-money spreads that have **widened past
   1c** (pre-game books sit pinned at 1c; in-play retail flow widens them to ~2c+). Evening US time.
   Prefer the highest-volume fixture on the slate (e.g. the Guadalajara/Chivas match).
2. `--auth-check` passes (creds alive; shows balance + positions).
3. You accept the ~$8 balance (or topped up); you've eyeballed any pre-existing open position.

## Run it (staged)

```bash
# 0. read-only: creds + balance + positions
uv run python -m ml.lp.lp_live --auth-check

# 1. confirm a Liga MX game is live + the book is in-play-wide (read-only)
uv run python -m ml.lp.soccer_screen --leagues MEX

# 2. (optional, one-time) validate the order create/list/cancel plumbing — a $0.02 unfillable bid
uv run python -m ml.lp.lp_live --test-order

# 3. LAUNCH — two processes, same window, same league:
#    (a) the paired capture (its own terminal) — the microstructure the correlation needs
uv run python -m ml.lp.ws_features --prefix KXLIGAMX --minutes 60

#    (b) the pilot maker (REST book; simplest/proven path for a first run)
uv run python -m ml.lp.lp_live --live --i-understand-live \
    --pilot KXLIGAMX --prefix KXLIGAMX --minutes 60
```

- `--pilot KXLIGAMX` = bypass the fail-closed FAMILY gate for Liga MX only (hard caps applied).
- `--prefix KXLIGAMX` = narrow the SELECTION universe to Liga MX (so rolls/switches stay in-league).
- `--minutes 60` = wall-clock; the bot rolls to a fresh Liga MX market as each resolves. Size a
  half (~45m+stoppage) or run across halftime; re-launch for the second half if it idles.
- Start the capture (a) a minute BEFORE the maker so the pre-fill board is logged.

## What to watch live

- The startup banner should read: `⚠ PILOT override active for ['KXLIGAMX'] — hard-capped (size 1,
  kill $5)`, and `quotable now: [] — none` (correct: nothing confirmed; only the pilot is quoting).
- It should quote ONLY `KXLIGAMX...TOTAL/SPREAD`. If it ever selects a non-Liga-MX or a GAME/WINNER
  ticker, stop — the gate is wrong.
- Per-market P&L + the running session total. Stop if it hits -$5 (it self-kills), or if the game
  ends / books go one-sided.
- Panic: `uv run python -m ml.lp.lp_live --cancel-all` cancels all resting orders.

## After the run — what it answers

```bash
uv run python -m ml.lp.lp_analyze                       # session P&L + markout, by config
uv run python -m ml.lp.realized_toxicity --by family    # LIGAMX/SPREAD realized fill-markout
uv run python -m ml.lp.breakeven --leagues MEX          # place the realized numbers on the curve
```

- **Capture-efficiency transfer:** is Liga MX realized capture near WC/SPREAD's κ≈0.28, or the
  capture-poor WC/TOTAL κ≈0.06? Feeds `realized_toxicity` + `breakeven`.
- **Toxicity transfer:** the fill-anchored `markout_c` for LIGAMX/SPREAD — is it ~WC's −0.13c or
  worse? (This metric SEES goals; the passive flow one doesn't.)
- **Correlation overlap:** the paired capture lands in S3 → next `dbt build` → `fct_ws_markout` /
  `fct_toxicity_by_family`; `edge_verdict` then shows Liga MX's PUBLIC flow-markout on the SAME
  market-time as our real fills — the first data point for validating the public instrument.
- When Liga MX accumulates enough clean days, `edge_verdict --emit` will promote it to CONFIRMED and
  the pilot override is no longer needed (the fail-closed gate lets it through on its own).

## Do NOT

- Run without `--pilot` expecting it to quote — it will idle (fail-closed, by design).
- Lead with club TOTAL — WC/TOTAL itself was near-breakeven; SPREAD is the candidate.
- Scale size or leave it unattended on a first pilot. This is a MEASUREMENT run, not a scaling run.
