"""THE EDGE VERDICT — is any market family actually makeable? Statistics, not vibes.

Reads the toxicity scoreboard (``fct_toxicity_by_family`` — per family per day) and, where we
have traded, the realized capture (``fct_lp_market_session``), and judges each family on TWO
adverse-selection axes:

  * FLOW axis (``markout_c``) — the flow-signed markout: FLOW-TOXIC if its day-block 95% CI sits
    ABOVE 0 (taker flow predicts price), FLOW-BENIGN if at/below ~0, else INCONCLUSIVE. This axis
    is BLIND to a jump not preceded by flow (a goal, a tennis point) — the dominant sports pick-off.
  * JUMP axis (``jump_c``) — the flow-INDEPENDENT goal/point pick-off (max(0,|move|-half-spread)):
    jump-TOXIC if its CI sits above JUMP_TOXIC_FLOOR, jump-BENIGN if below. This is what catches
    the books that read flow-benign yet bleed to jumps (MLB-GAME, ITF — the review's blind spot).
  * BREADTH (``breadth``) — a market ATTRIBUTE, not an axis: SINGLE_NAME (a named team/player's
    outcome) vs BROAD (aggregate over/under). Per Bartlett & O'Hara (2026, 41.6M Kalshi trades)
    one-sided-flow toxicity predicts maker losses in SINGLE_NAME markets but NOT BROAD — i.e. the
    FLOW axis only has power for SINGLE_NAME. Reported + diagnosed here (does the localization
    replicate on our data?); the gate stays flow+jump for now, and breadth-aware gating (relax the
    flow requirement for BROAD families, where a one-sided flow reading isn't toxicity) is the
    MEASURED next step, not assumed.

Discipline (the project's own standards, applied to making):
  * The resampling unit is the DAY (day-block bootstrap) — thousands of snapshots within one
    game day are one observation of one regime, not thousands.
  * Split-half stability is reported: does the first half of days agree in sign with the second?
  * The known-toxic controls (ITF/ATP MATCH, MLB/NBA GAME) are the instrument check: each must read
    toxic on AT LEAST ONE axis; benign on BOTH once sufficient = the label is broken, distrust all.
  * Multiple comparisons: with ~dozens of families, expect false "benign" reads at the margin —
    the PRE-COMMITTED primary hypothesis is soccer TOTAL/SPREAD; everything else is exploratory.
  * Benign flow is necessary, NOT sufficient. EDGE = FLOW-BENIGN AND jump-BENIGN AND realized
    capture > 0 on our own fills (stage 2). ``--emit`` writes that as the fail-closed tier.

Usage:
    uv run python -m core.maker.edge_verdict                  # verdict over all captured days
    uv run python -m core.maker.edge_verdict --min-days 3     # relax the floor (early look)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

import numpy as np
import numpy.typing as npt
import pandas as pd
from dotenv import load_dotenv
from pyathena import connect

MIN_DAYS = 8  # ET days; a benign call on <8 is too fragile (split-half is a coin flip at 5).
#               Raised from 5 per the review. Pass --min-days for an early exploratory look.
MIN_FLOW_OBS = 500  # fewer flow-bearing snapshots = noise
BENIGN_CI_HI = 0.10  # cents; CI upper bound must sit at/below ~0 to call flow benign
# The flow-INDEPENDENT jump axis (avg_jump_pickoff_c) reads TOXIC above this floor. Calibrated
# on the first captures: the known-jumpy controls (ATP/ITF MATCH, *_GAME) sit at 0.4-1.4c while
# soccer TOTAL sits <0.08c — a ~6x gap, so 0.25c cleanly separates goal/point pick-off from calm.
JUMP_TOXIC_FLOOR = 0.25
N_BOOT = 4000
# Instrument controls: known-toxic families. On the FLOW axis some read benign (ITF/MLB-GAME flow
# barely leads price) yet a maker still bleeds to point/score JUMPS — so each control must read
# toxic on AT LEAST ONE axis; benign on BOTH = the label is broken, not the market benign.
KNOWN_TOXIC = {("ITF", "MATCH"), ("MLB", "GAME"), ("NBA", "GAME"), ("ATP", "MATCH")}

FloatArr = npt.NDArray[np.float64]

TOX_QUERY = """
select sport, market_type, breadth, capture_day, n_snapshots, n_markets, n_flow_obs,
       avg_flow_markout_c, avg_jump_pickoff_c
from crypto_marts.fct_toxicity_by_family
where n_flow_obs > 0
"""

# European club-soccer leagues the captured data shows share ONE SPREAD/TOTAL toxicity
# profile (per-league jump 0.02–0.17c, all sub-0.25 — big-five + UCL/UEL + minor European).
# Pooling them into one family reaches the 8-day floor via their DIFFERENT kickoff schedules
# (La Liga Fri / EPL Sat / Serie A Sun / UCL midweek) far faster than any single league, with
# tighter CIs. WC (the benchmark) and Americas leagues (MLS/LigaMX/Brasileiro) stay separate.
CLUB_SOCCER_POOL = frozenset({
    "EPL", "LALIGA", "SERIEA", "BUNDESLIGA", "LIGUE1", "UCL_UEL", "SOCCER_OTHER",
})
CLUB_SOCCER_LABEL = "CLUB_SOCCER"


def pool_club_soccer(tox: pd.DataFrame) -> pd.DataFrame:
    """Re-aggregate the European club leagues into one CLUB_SOCCER sport per
    (market_type, capture_day): obs-weighted axis means (flow by n_flow_obs, jump by
    n_snapshots), so the pooled per-day rows feed the SAME day-block bootstrap unchanged."""
    mask = tox["sport"].isin(CLUB_SOCCER_POOL)
    if not mask.any():
        return tox
    club = tox[mask].copy()
    club["sport"] = CLUB_SOCCER_LABEL
    club["_fw"] = club["avg_flow_markout_c"] * club["n_flow_obs"]
    club["_jw"] = club["avg_jump_pickoff_c"] * club["n_snapshots"]
    gp = club.groupby(["sport", "market_type", "capture_day"], as_index=False).agg(
        breadth=("breadth", "first"),
        n_snapshots=("n_snapshots", "sum"),
        n_markets=("n_markets", "sum"),
        n_flow_obs=("n_flow_obs", "sum"),
        _fw=("_fw", "sum"),
        _jw=("_jw", "sum"),
    )
    gp["avg_flow_markout_c"] = gp["_fw"] / gp["n_flow_obs"].where(gp["n_flow_obs"] > 0, 1)
    gp["avg_jump_pickoff_c"] = gp["_jw"] / gp["n_snapshots"].where(gp["n_snapshots"] > 0, 1)
    gp = gp.drop(columns=["_fw", "_jw"])
    return pd.concat([tox[~mask], gp], ignore_index=True)


def pool_club_soccer_realized(realized: pd.DataFrame) -> pd.DataFrame:
    """Pool realized fills the same way: sum capture + fills across club leagues, recompute
    the fills-weighted markout. (No club fills yet -> the pooled family caps at CANDIDATE.)"""
    mask = realized["sport"].isin(CLUB_SOCCER_POOL)
    if not mask.any():
        return realized
    club = realized[mask].copy()
    club["sport"] = CLUB_SOCCER_LABEL
    club["_mw"] = club["realized_markout_c"] * club["fills"]
    gp = club.groupby(["sport", "market_type"], as_index=False).agg(
        traded_days=("traded_days", "sum"),
        fills=("fills", "sum"),
        capture_usd=("capture_usd", "sum"),
        _mw=("_mw", "sum"),
    )
    gp["realized_markout_c"] = gp["_mw"] / gp["fills"].where(gp["fills"] > 0, 1)
    gp = gp.drop(columns=["_mw"])
    return pd.concat([realized[~mask], gp], ignore_index=True)


REALIZED_QUERY = """
select sport, market_type,
       count(distinct et_day)                             as traded_days,
       sum(n_fills)                                       as fills,
       sum(spread_capture)                                as capture_usd,
       sum(mean_markout_c * n_fills) / nullif(sum(n_fills), 0) as realized_markout_c
from crypto_marts.fct_lp_market_session
group by sport, market_type
"""


def day_block_ci(day_means: FloatArr, rng: np.random.Generator) -> tuple[float, float, float]:
    """UNWEIGHTED mean of per-day means (each ET day is ONE observation) + a 95% CI from
    resampling days with replacement. Unweighted is deliberate: weighting by snapshot / flow
    count re-imports within-day pseudo-replication — one heavy-volume day would dominate the
    estimate and shrink the CI, when it is really one regime observation. Matches the primary
    estimator in realized_toxicity.py."""
    n = len(day_means)
    point = float(day_means.mean())
    stats = np.empty(N_BOOT)
    for b in range(N_BOOT):
        stats[b] = day_means[rng.integers(0, n, n)].mean()
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return point, float(lo), float(hi)


def split_half_sign(day_means: FloatArr) -> str:
    """Do the two halves of the (chronological) day series agree in sign?"""
    if len(day_means) < 4:
        return "n/a"
    half = len(day_means) // 2
    a, b = float(np.mean(day_means[:half])), float(np.mean(day_means[half:]))
    return "agree" if np.sign(a) == np.sign(b) else "DISAGREE"


def verdict_for(
    days: int, flow_obs: int, lo: float, hi: float, min_days: int, min_flow: int
) -> str:
    if days < min_days or flow_obs < min_flow:
        return "INSUFFICIENT"
    if lo > 0:
        return "FLOW-TOXIC"
    if hi <= BENIGN_CI_HI:
        return "FLOW-BENIGN"
    return "INCONCLUSIVE"


def jump_state_for(days: int, jlo: float, jhi: float, min_days: int) -> str:
    """The flow-INDEPENDENT jump verdict from its day-block CI vs JUMP_TOXIC_FLOOR:
    TOXIC (CI wholly above the floor = goal/point pick-off), BENIGN (CI wholly below), else
    INCONCLUSIVE. INSUFF under the day floor."""
    if days < min_days:
        return "INSUFF"
    if jlo > JUMP_TOXIC_FLOOR:
        return "TOXIC"
    if jhi < JUMP_TOXIC_FLOOR:
        return "BENIGN"
    return "INCONCLUSIVE"


def tier_for(flow_verdict: str, jump_state: str, capture_usd: float | None) -> str | None:
    """Trading tier from BOTH toxicity axes + our OWN realized capture. A family must be benign
    on the flow axis AND not jump-toxic; the realized column then decides how far to trust it:
      * CONFIRMED     — flow-benign, jump-benign, AND positive realized capture = quotable.
      * CANDIDATE     — flow-benign + not jump-toxic but not yet CONFIRMED (jump inconclusive OR
                        never traded) = quote ONLY as a --pilot to gather evidence, hard-capped.
      * CONTRADICTION — flow-benign + jump-benign but our fills bled (capture <= 0) = do NOT quote.
    Flow-toxic OR JUMP-TOXIC => None (absent from the file = never quotable). The jump gate is the
    fix for the review's blind spot: a book can read flow-benign while bleeding to goals/points."""
    if flow_verdict != "FLOW-BENIGN" or jump_state == "TOXIC":
        return None
    if jump_state != "BENIGN":
        return "CANDIDATE"  # jump not confirmed benign -> pilot-only, keep collecting
    if capture_usd is None:
        return "CANDIDATE"
    return "CONFIRMED" if capture_usd > 0 else "CONTRADICTION"


def emit_quotable(rows: list[dict[str, object]], as_of_day: str, meta: dict[str, object],
                  path: str) -> list[str]:
    """Write the machine-readable verdict the bot reads (fail-CLOSED). Returns the CONFIRMED
    families (the default-quotable allowlist). `as_of_day` is the freshness anchor: the bot
    refuses the whole file if it is stale, so a dead capture pipeline can't leave families
    quotable forever."""
    families = {}
    quotable: list[str] = []
    for r in rows:
        tier = r.get("tier")
        if tier is None:
            continue  # not benign -> omit entirely (absence = not quotable)
        fam = str(r["family"])
        families[fam] = {
            "tier": tier, "verdict": r["verdict"], "breadth": r["breadth"],
            "markout_c": r["markout_c"],
            "ci": r["ci_tuple"], "days": r["days"], "flow_obs": r["flow_obs"],
            "jump_pickoff_c": r["jump_c"], "jump_state": r["jump"],
            "realized_capture_usd": r["capture_usd"], "realized_markout_c": r["realized_markout_c"],
        }
        if tier == "CONFIRMED":
            quotable.append(fam)
    doc = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "as_of_capture_day": as_of_day,
        **meta,
        "quotable": sorted(quotable),
        "families": families,
    }
    with open(path, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return sorted(quotable)


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-family maker-edge verdict from captured data")
    ap.add_argument("--min-days", type=int, default=MIN_DAYS)
    ap.add_argument("--min-flow-obs", type=int, default=MIN_FLOW_OBS)
    ap.add_argument("--emit", metavar="PATH", default=None,
                    help="write the machine-readable quotable_families.json the bot reads")
    ap.add_argument("--pool-club-soccer", action="store_true",
                    help="re-aggregate European club leagues into one CLUB_SOCCER family "
                         "(reaches the day-floor via their different kickoff schedules)")
    args = ap.parse_args()

    load_dotenv()
    conn = connect(
        work_group=os.environ.get("ATHENA_WORKGROUP", "crypto_wg"),
        s3_staging_dir=os.environ["ATHENA_S3_STAGING_DIR"],
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    tox = pd.read_sql(TOX_QUERY, conn)
    realized = pd.read_sql(REALIZED_QUERY, conn)
    if args.pool_club_soccer:
        tox = pool_club_soccer(tox)
        realized = pool_club_soccer_realized(realized)
    if tox.empty:
        print("fct_toxicity_by_family is EMPTY — no capture data yet. Run/dispatch the WS "
              "capture during live games (docs/setup/09), then `dbt build`, then re-run this.")
        return 0

    realized_by_fam = {
        (r["sport"], r["market_type"]): r for _, r in realized.iterrows()
    }
    rng = np.random.default_rng(7)  # fixed seed: the verdict must be reproducible

    rows: list[dict[str, object]] = []
    for (sport, mtype), g in tox.groupby(["sport", "market_type"]):
        g = g.sort_values("capture_day")
        days = len(g)
        breadth = str(g["breadth"].iloc[0])  # constant within a (sport, market_type) group
        # flow axis (signed by trailing taker flow) — unweighted per-day
        means = g["avg_flow_markout_c"].to_numpy(dtype=np.float64)
        point, lo, hi = day_block_ci(means, rng)
        flow_obs = int(g["n_flow_obs"].sum())
        v = verdict_for(days, flow_obs, lo, hi, args.min_days, args.min_flow_obs)
        # jump axis (flow-independent; sees goal/point pick-off the flow axis is blind to)
        jmeans = g["avg_jump_pickoff_c"].to_numpy(dtype=np.float64)
        jpoint, jlo, jhi = day_block_ci(jmeans, rng)
        jstate = jump_state_for(days, jlo, jhi, args.min_days)
        real = realized_by_fam.get((str(sport), str(mtype)))
        capture_usd = float(real["capture_usd"]) if real is not None else None
        realized_mk = (
            round(float(real["realized_markout_c"]), 3)
            if real is not None and pd.notna(real["realized_markout_c"]) else None
        )
        rows.append({
            "family": f"{sport}/{mtype}",
            "breadth": breadth,
            "days": days,
            "flow_obs": flow_obs,
            "markout_c": round(point, 3),
            "ci": f"[{lo:+.2f},{hi:+.2f}]",
            "ci_tuple": [round(lo, 3), round(hi, 3)],
            "jump_c": round(jpoint, 3),
            "jump": jstate,
            "split_half": split_half_sign(means),
            "control": "CONTROL" if (str(sport), str(mtype)) in KNOWN_TOXIC else "",
            "realized_$": f"{capture_usd:+.0f}" if capture_usd is not None else "-",
            "capture_usd": None if capture_usd is None else round(capture_usd, 2),
            "realized_markout_c": realized_mk,
            "tier": tier_for(v, jstate, capture_usd),
            "verdict": v,
        })

    out = pd.DataFrame(rows).sort_values(["verdict", "markout_c"])
    display_cols = ["family", "breadth", "days", "flow_obs", "markout_c", "ci", "jump_c", "jump",
                    "control", "realized_$", "tier", "verdict"]
    print("=" * 100)
    print("EDGE VERDICT — two toxicity axes per family (day-block bootstrap 95% CI, cents)")
    print("  markout_c: flow-signed (>0 = flow leads price). jump_c: flow-independent goal/point")
    print(f"  pick-off (TOXIC > {JUMP_TOXIC_FLOOR}c). Controls MUST read toxic on >=1 axis.")
    print("=" * 100)
    print(out[display_cols].fillna("-").to_string(index=False,
                        formatters={"markout_c": lambda x: f"{x:+.2f}"}))

    n_eval = int((out["verdict"] != "INSUFFICIENT").sum())
    controls = out[out["control"] == "CONTROL"]
    # a control is only "passing" if it reads toxic on SOME axis; benign flow AND non-toxic jump
    # = the instrument is blind, not the market benign.
    bad_controls = controls[(controls["verdict"] == "FLOW-BENIGN") & (controls["jump"] != "TOXIC")]
    print(f"\nfamilies evaluated: {n_eval} — expect ~{max(1, n_eval // 20)} false reads at 95%; "
          "the PRE-COMMITTED hypothesis is soccer TOTAL/SPREAD, the rest is exploratory.")
    if not bad_controls.empty:
        print("⚠ INSTRUMENT FAILURE: known-toxic control(s) read benign on BOTH axes — "
              f"{', '.join(bad_controls['family'])}. Distrust every verdict above; "
              "check the markout horizon / capture density before believing anything.")

    # --- BREADTH diagnostic (Bartlett & O'Hara 2026): does flow-toxicity localize to SINGLE_NAME? -
    # If SINGLE_NAME reads materially more flow-toxic than BROAD, the flow gate should apply to
    # SINGLE_NAME only — a BROAD family failing on the flow axis ALONE may be a false refusal. This
    # is the measured evidence for (or against) the breadth-aware gate; the gate is unchanged today.
    evaluated = out[out["verdict"] != "INSUFFICIENT"]
    print("\nBREADTH — is the FLOW axis's power concentrated in SINGLE_NAME markets? (lit: yes)")
    for b in ("SINGLE_NAME", "BROAD"):
        sub = evaluated[evaluated["breadth"] == b]
        if sub.empty:
            print(f"  {b:11} — no families evaluated yet")
            continue
        mean_flow = float(sub["markout_c"].mean())
        frac_toxic = float((sub["verdict"] == "FLOW-TOXIC").mean())
        mean_jump = float(sub["jump_c"].mean())
        print(f"  {b:11} n={len(sub):>2}  mean flow-markout {mean_flow:+.3f}c  "
              f"flow-TOXIC {frac_toxic:.0%}  mean jump {mean_jump:.3f}c")
    print("  (A BROAD family failing on flow ALONE may be a false refusal — the measured basis "
          "for breadth-aware gating, still a proposal, not yet wired into the tier.)")

    print("\nBenign flow is necessary, NOT sufficient: EDGE = flow-benign AND jump-benign AND "
          "realized capture > 0 on our own fills. The jump axis is why a book that looks "
          "flow-benign can still be refused (it bleeds to goals/points).")

    if args.emit:
        as_of_day = str(pd.to_datetime(tox["capture_day"]).max().date())
        meta = {"min_days": args.min_days, "min_flow_obs": args.min_flow_obs,
                "benign_ci_hi": BENIGN_CI_HI}
        quotable = emit_quotable(rows, as_of_day, meta, args.emit)
        n_cand = sum(1 for r in rows if r.get("tier") == "CANDIDATE")
        n_contra = sum(1 for r in rows if r.get("tier") == "CONTRADICTION")
        print(f"\n→ wrote {args.emit} (as-of capture day {as_of_day}): "
              f"{len(quotable)} CONFIRMED-quotable {quotable or '[]'}, "
              f"{n_cand} CANDIDATE (pilot-only), {n_contra} CONTRADICTION (refused). "
              "The bot reads this fail-CLOSED and refuses anything not freshly CONFIRMED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
