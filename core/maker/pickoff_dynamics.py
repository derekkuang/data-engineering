"""Is there a microstructure WARNING before the pick-off jump, or is it a news surprise?

The soccer market-making edge lives or dies on adverse selection: a resting maker captures the
spread until a scoring event gaps the mid PAST the half-spread and picks them off. The one lever
that makes the edge protectable is a LEADING signal — if one-sided flow / a volatility spike
precedes the jump, the maker can pull the quote; if the jump is simultaneous with the news (a
goal), the maker's only defense is a wider spread. We can't answer this from soccer alone (goals
are too rare), but MLB at-bats and tennis points are FREQUENT, so the toxic non-soccer capture is
the best data on the planet for characterizing the pick-off dynamics — which then transfer to the
soccer pilot's pull/quote-width rule.

Data: `crypto_marts.fct_ws_markout`. Each 5s snapshot carries TRAILING microstructure features over
[t-60s, t] (imbalance, signed_flow_1m, midvol_1m, trades_1m, midmove_1m, taker_buy_frac) — all
observable AT decision time t — and the FORWARD 30s outcome: `jump_pickoff_c = max(0, |mid move| -
spread/2)` = the part of the move that runs past a touch-resting maker's half-spread capture. A
"pick-off EVENT" is a snapshot whose forward jump clears a threshold; "calm" = the mid stayed within
the half-spread (jump = 0).

Three decision-relevant reads, per frequent-scoring sport:
  1. WARNING? — do the trailing features SEPARATE pick-off from calm? Univariate AUC of each
     feature predicting the jump (AUC >> 0.5 = a usable leading signal → a pull rule is viable).
  2. FLOW-LED vs NEWS? — among pick-offs, is `flow_signed_markout_c` (trailing-flow-signed forward
     move) systematically POSITIVE (informed flow front-ran the jump → warn-able) or ~0 (the jump
     is independent of prior flow → a news surprise no maker can dodge)? Day-block bootstrap CI.
  3. SPREAD CUSHION — the |jump| magnitude distribution → what half-spread covers X% of the jumps
     (the quote-width the maker needs when there is no warning).

Discipline: 5s snapshots overlap (a single scoring event spawns a cluster as the 30s window slides),
so the effective sample is FAR below the row count — the flow-led CI is day-block bootstrapped by
(sport, ET day), and the AUC/contrast are read as directional, not p-valued.

Usage:
    uv run python -m core.maker.pickoff_dynamics
    uv run python -m core.maker.pickoff_dynamics --days 21 --jump-thresh 3.0
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from pyathena import connect
from sklearn.metrics import roc_auc_score

from core.maker.classify import sport
from core.maker.edge_verdict import day_block_ci

# Frequent-scoring families = where the pick-off EVENTS are dense enough to characterize.
FREQUENT_PREFIXES = ("KXMLB", "KXATP", "KXITF", "KXWTA", "KXWNBA", "KXNBA")
# Leading features observable at decision time t (magnitude / one-sidedness measures a maker could
# act on) — a directional field only warns via its |magnitude|. These column names are materialized
# in load() (the |.| ones precomputed), so warning_table just reads them by name.
LEADING = ("|imbalance|", "|signed_flow_1m|", "midvol_1m", "trades_1m",
           "|midmove_1m|", "|taker_buy-.5|")


def _query(days: int) -> str:
    like = " or ".join(f"market_ticker like '{p}%'" for p in FREQUENT_PREFIXES)
    since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    return f"""
    select market_ticker, snapshot_at, spread_c, imbalance, signed_flow_1m, taker_buy_frac,
           trades_1m, midvol_1m, midmove_1m, jump_pickoff_c, flow_signed_markout_c,
           abs_fwd_move_c, fwd_mid_move_c
    from crypto_marts.fct_ws_markout
    where ({like}) and partition_date >= '{since}'
    """


def load(conn: object, days: int, jump_thresh: float) -> pd.DataFrame:
    df = pd.read_sql(_query(days), conn)
    df["sport"] = df["market_ticker"].map(sport)
    df["et_day"] = (
        pd.to_datetime(df["snapshot_at"], utc=True)
        .dt.tz_convert("America/New_York").dt.date.astype(str)
    )
    # leading (decision-time) magnitudes
    df["|imbalance|"] = df["imbalance"].abs()
    df["|signed_flow_1m|"] = df["signed_flow_1m"].abs()
    df["|midmove_1m|"] = df["midmove_1m"].abs()
    df["|taker_buy-.5|"] = (df["taker_buy_frac"] - 0.5).abs()
    # event labels
    df["is_pickoff"] = df["jump_pickoff_c"] >= jump_thresh
    df["is_calm"] = df["jump_pickoff_c"] <= 0.0
    return df


def warning_table(sub: pd.DataFrame) -> pd.DataFrame:
    """Per leading feature: mean before a pick-off vs before calm, and the AUC predicting a jump.
    AUC >> 0.5 = the feature gives a usable pre-jump warning."""
    y = sub["is_pickoff"].to_numpy(dtype=int)
    po_mask, calm_mask = sub["is_pickoff"].to_numpy(), sub["is_calm"].to_numpy()
    rows = []
    for name in LEADING:
        f = sub[name].to_numpy(dtype=float)
        po = float(np.nanmean(f[po_mask])) if po_mask.any() else float("nan")
        calm = float(np.nanmean(f[calm_mask])) if calm_mask.any() else float("nan")
        auc = float(roc_auc_score(y, np.nan_to_num(f))) if 0 < y.sum() < len(y) else float("nan")
        rows.append({"feature": name, "pre_pickoff": po, "pre_calm": calm,
                     "ratio": po / calm if calm else float("nan"), "auc": auc})
    return pd.DataFrame(rows)


def flow_led(sub: pd.DataFrame, rng: np.random.Generator) -> tuple[float, float, float, float]:
    """Among pick-off events: mean flow_signed_markout_c (>0 = trailing flow front-ran the jump =
    warn-able; ~0 = news) via a day-block CI, plus directional hit rate sign(flow)==sign(move)."""
    po = sub[sub["is_pickoff"]]
    if po.empty:
        return float("nan"), float("nan"), float("nan"), float("nan")
    day_means = po.groupby("et_day")["flow_signed_markout_c"].mean().to_numpy(dtype=float)
    point, lo, hi = day_block_ci(day_means, rng)
    flowed = po[po["signed_flow_1m"] != 0]
    hit = float((np.sign(flowed["signed_flow_1m"]) == np.sign(flowed["fwd_mid_move_c"])).mean())
    return point, lo, hi, hit


def cushion(sub: pd.DataFrame) -> dict[str, float]:
    """The |forward move| magnitude of pick-off events → the half-spread a maker needs to cover
    them. p50/p75/p90 of |move|; a maker resting at half-spread H is picked off when |move| > H."""
    mv = sub.loc[sub["is_pickoff"], "abs_fwd_move_c"].to_numpy(dtype=float)
    if mv.size == 0:
        return {}
    return {q: float(np.percentile(mv, p)) for q, p in {"p50": 50, "p75": 75, "p90": 90}.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description="Pick-off dynamics: is there a pre-jump warning?")
    ap.add_argument("--days", type=int, default=21, help="partition_date lookback")
    ap.add_argument("--jump-thresh", type=float, default=2.0, help="cents past half-spread = jump")
    ap.add_argument("--min-events", type=int, default=200, help="skip sports with fewer pick-offs")
    args = ap.parse_args()

    load_dotenv()
    conn = connect(
        work_group=os.environ.get("ATHENA_WORKGROUP", "crypto_wg"),
        s3_staging_dir=os.environ["ATHENA_S3_STAGING_DIR"],
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    df = load(conn, args.days, args.jump_thresh)
    rng = np.random.default_rng(7)
    print(f"loaded {len(df):,} snapshots over {df['et_day'].nunique()} ET days; "
          f"pick-off = jump_pickoff_c >= {args.jump_thresh}c "
          f"({df['is_pickoff'].mean():.1%} of snapshots), calm = {df['is_calm'].mean():.1%}")

    for sp in sorted(df["sport"].unique()):
        sub = df[df["sport"] == sp]
        n_po = int(sub["is_pickoff"].sum())
        if n_po < args.min_events:
            continue
        print("\n" + "=" * 92)
        print(f"{sp} — {len(sub):,} snapshots, {n_po:,} pick-offs, {sub['et_day'].nunique()} days")
        print("=" * 92)
        wt = warning_table(sub)
        print("  WARNING? leading feature at decision time t vs the next-30s jump:")
        print(wt.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
        point, lo, hi, hit = flow_led(sub, rng)
        led = "FLOW-LED (warn-able)" if lo > 0.1 else "NEWS-like (no flow warning)"
        print(f"\n  FLOW-LED vs NEWS: mean flow-signed markout on jumps {point:+.2f}c "
              f"[{lo:+.2f},{hi:+.2f}] (day-block) → {led}")
        print(f"    directional hit rate sign(flow)==sign(jump): {hit:.0%} (50% = no info)")
        cu = cushion(sub)
        if cu:
            print(f"  SPREAD CUSHION: pick-off |move| p50/p75/p90 = "
                  f"{cu['p50']:.1f}/{cu['p75']:.1f}/{cu['p90']:.1f}c "
                  f"→ half-spread ~{cu['p90']:.0f}c dodges 90% of jumps")

    print("\n" + "-" * 92)
    print("READ: high AUC on |signed_flow|/|imbalance| AND a CI-positive flow-signed markout = the")
    print("jump is FRONT-RUN by flow → a maker can pull (edge protectable). AUC~0.5 and a")
    print("flow-signed markout CI spanning 0 = a NEWS surprise → the only defense is the")
    print("spread cushion above (quote at least that wide, or expect to eat the pick-off).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
