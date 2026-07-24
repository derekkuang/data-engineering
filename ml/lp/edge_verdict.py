"""THE EDGE VERDICT — is any market family actually makeable? Statistics, not vibes.

Reads the toxicity scoreboard (``fct_toxicity_by_family`` — per family per day, flow-signed
markout over flow-bearing snapshots) and, where we have traded, the realized capture
(``fct_lp_market_session``), and prints a per-family verdict:

  * FLOW-TOXIC    — day-block 95% CI of the flow-signed markout sits ABOVE 0: taker flow
                    predicts price; a resting maker gets picked off. Do not quote.
  * FLOW-BENIGN   — the CI sits at/below ~0: uninformed flow; the stage-2 candidate set.
  * INCONCLUSIVE  — CI straddles the line; keep collecting.
  * INSUFFICIENT  — under the day / flow-observation floors; no verdict yet.

Discipline (the project's own standards, applied to making):
  * The resampling unit is the DAY (day-block bootstrap) — thousands of snapshots within one
    game day are one observation of one regime, not thousands.
  * Split-half stability is reported: does the first half of days agree in sign with the second?
  * The known-toxic controls (ITF, MLB/NBA GAME) are the instrument check: if they don't read
    toxic once sufficient, DISTRUST every benign verdict (the label is broken, not the market).
  * Multiple comparisons: with ~dozens of families, expect false "benign" reads at the margin —
    the PRE-COMMITTED primary hypothesis is soccer TOTAL/SPREAD; everything else is exploratory.
  * FLOW-BENIGN is necessary, NOT sufficient, for an edge. EDGE = benign flow AND realized
    capture > 0 on our own fills (stage 2). The realized column shows where that evidence exists.

Usage:
    uv run python -m ml.lp.edge_verdict                  # verdict over all captured days
    uv run python -m ml.lp.edge_verdict --min-days 3     # relax the floor (early look)
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

MIN_DAYS = 5  # fewer days = one regime; no verdict
MIN_FLOW_OBS = 500  # fewer flow-bearing snapshots = noise
BENIGN_CI_HI = 0.10  # cents; CI upper bound must sit at/below ~0 to call flow benign
N_BOOT = 4000
KNOWN_TOXIC = {("ITF", "MATCH"), ("MLB", "GAME"), ("NBA", "GAME")}  # instrument controls

FloatArr = npt.NDArray[np.float64]

TOX_QUERY = """
select sport, market_type, capture_day, n_snapshots, n_markets, n_flow_obs,
       avg_flow_markout_c
from crypto_marts.fct_toxicity_by_family
where n_flow_obs > 0
"""

REALIZED_QUERY = """
select sport, market_type,
       count(distinct et_day)                             as traded_days,
       sum(n_fills)                                       as fills,
       sum(spread_capture)                                as capture_usd,
       sum(mean_markout_c * n_fills) / nullif(sum(n_fills), 0) as realized_markout_c
from crypto_marts.fct_lp_market_session
group by sport, market_type
"""


def day_block_ci(
    day_means: FloatArr, day_weights: FloatArr, rng: np.random.Generator
) -> tuple[float, float, float]:
    """Weighted mean of per-day means + a 95% CI from resampling DAYS with replacement."""
    n = len(day_means)
    point = float(np.average(day_means, weights=day_weights))
    stats = np.empty(N_BOOT)
    for b in range(N_BOOT):
        idx = rng.integers(0, n, n)
        stats[b] = np.average(day_means[idx], weights=day_weights[idx])
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


def tier_for(verdict: str, capture_usd: float | None) -> str | None:
    """Trading tier from the flow verdict + our OWN realized capture. Only FLOW-BENIGN
    families are eligible; the realized column decides how far to trust them:
      * CONFIRMED     — benign flow AND we made positive spread capture making it = quotable.
      * CANDIDATE     — benign flow, never traded (no fills) = quote ONLY as a --pilot to
                        gather the first realized evidence, hard-capped.
      * CONTRADICTION — benign flow but our fills bled (capture <= 0) = do NOT quote; the
                        flow label and the money disagree (the review's contradiction alarm).
    Non-benign families return None (absent from the file = never quotable, fail-closed)."""
    if verdict != "FLOW-BENIGN":
        return None
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
            "tier": tier, "verdict": r["verdict"], "markout_c": r["markout_c"],
            "ci": r["ci_tuple"], "days": r["days"], "flow_obs": r["flow_obs"],
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
    args = ap.parse_args()

    load_dotenv()
    conn = connect(
        work_group=os.environ.get("ATHENA_WORKGROUP", "crypto_wg"),
        s3_staging_dir=os.environ["ATHENA_S3_STAGING_DIR"],
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    tox = pd.read_sql(TOX_QUERY, conn)
    realized = pd.read_sql(REALIZED_QUERY, conn)
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
        means = g["avg_flow_markout_c"].to_numpy(dtype=np.float64)
        weights = g["n_flow_obs"].to_numpy(dtype=np.float64)
        point, lo, hi = day_block_ci(means, weights, rng)
        days, flow_obs = len(g), int(g["n_flow_obs"].sum())
        v = verdict_for(days, flow_obs, lo, hi, args.min_days, args.min_flow_obs)
        real = realized_by_fam.get((str(sport), str(mtype)))
        capture_usd = float(real["capture_usd"]) if real is not None else None
        realized_mk = (
            round(float(real["realized_markout_c"]), 3)
            if real is not None and pd.notna(real["realized_markout_c"]) else None
        )
        rows.append({
            "family": f"{sport}/{mtype}",
            "days": days,
            "flow_obs": flow_obs,
            "markout_c": round(point, 3),
            "ci": f"[{lo:+.2f},{hi:+.2f}]",
            "ci_tuple": [round(lo, 3), round(hi, 3)],
            "split_half": split_half_sign(means),
            "control": "CONTROL" if (str(sport), str(mtype)) in KNOWN_TOXIC else "",
            "realized_$": f"{capture_usd:+.0f}" if capture_usd is not None else "-",
            "capture_usd": None if capture_usd is None else round(capture_usd, 2),
            "realized_markout_c": realized_mk,
            "tier": tier_for(v, capture_usd),
            "verdict": v,
        })

    out = pd.DataFrame(rows).sort_values(["verdict", "markout_c"])
    display_cols = ["family", "days", "flow_obs", "markout_c", "ci", "split_half",
                    "control", "realized_$", "tier", "verdict"]
    print("=" * 100)
    print("EDGE VERDICT — flow-signed markout per family (day-block bootstrap 95% CI, cents)")
    print("  >0 = taker flow leads price = maker gets picked off. Controls MUST read toxic.")
    print("=" * 100)
    print(out[display_cols].fillna("-").to_string(index=False,
                        formatters={"markout_c": lambda x: f"{x:+.2f}"}))

    n_eval = int((out["verdict"] != "INSUFFICIENT").sum())
    controls = out[out["control"] == "CONTROL"]
    bad_controls = controls[controls["verdict"] == "FLOW-BENIGN"]
    print(f"\nfamilies evaluated: {n_eval} — expect ~{max(1, n_eval // 20)} false reads at 95%; "
          "the PRE-COMMITTED hypothesis is soccer TOTAL/SPREAD, the rest is exploratory.")
    if not bad_controls.empty:
        print("⚠ INSTRUMENT FAILURE: known-toxic control(s) read FLOW-BENIGN — "
              f"{', '.join(bad_controls['family'])}. Distrust every verdict above; "
              "check the markout horizon / capture density before believing anything.")
    print("\nFLOW-BENIGN is necessary, NOT sufficient: EDGE = benign flow + realized capture "
          "> 0 on our own fills (stage 2 = run the bot on benign families; judged in "
          "fct_lp_daily with the same day-block discipline).")

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
