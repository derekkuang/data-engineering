"""GROUND TRUTH: realized per-family maker toxicity from our OWN fills.

This is the load-bearing measurement the whole toxicity apparatus is ultimately validated
against. Every fill the bot ever made carries a fill-anchored, contemporaneously-signed 30s
markout (`markout_c` in data/lp_fills.csv): the mid move over the 30s AFTER our fill, signed so
that <0 = the price moved against us = we were picked off (toxic), >=0 = benign (we kept the
spread). That is exactly the quantity the public WS flow-signed markout is a PROXY for — but
this is the real thing, measured on real money, at the fill instant, on our actual side.

Why this and not the public `fct_ws_markout`: the public instrument (a) measures trailing-flow
momentum, not fill-synchronous adverse selection, and (b) has no capture data for the WC period
anyway (it began 2026-07-22; our fills are 2026-06-16..27). So the public-vs-private CORRELATION
is not yet possible — it needs a live pilot overlapping the capture. Until then, THIS is the
family-selection evidence to trust, and it is what the public metric must reproduce once they
overlap.

Method (the project's standard, applied correctly):
  * The resampling unit is the ET DAY. PRIMARY estimate = unweighted mean of per-day mean
    markouts (each day counts once), with a day-block bootstrap 95% CI. A fill-weighted figure
    is reported as a sensitivity, and we flag when one day carries >40% of a family's fills
    (then the CI is effectively that day, per the review).
  * Families are (sport, market_type) using the SAME classifier the dbt marts should use
    (soccer-aware — the club leagues the post-WC hypothesis lives on). Kept in one place here
    as the canonical Python classifier.

Usage:
    uv run python -m core.maker.realized_toxicity
    uv run python -m core.maker.realized_toxicity --by sport   # roll up to sport only
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import cast

import numpy as np

from core.maker.classify import market_type, sport

FILLS_CSV = "data/lp_fills.csv"
ET = timezone(timedelta(hours=-4))  # EDT — the WC fills are all June; matches lp_analyze
N_BOOT = 5000
MIN_DAYS = 3  # fewer ET days than this = INSUFFICIENT, no CI


def _et_day(ts_utc: str) -> str:
    return datetime.fromisoformat(ts_utc).astimezone(ET).date().isoformat()


def dayblock_ci(
    per_day_mean: list[float], rng: np.random.Generator
) -> tuple[float, float, float]:
    """Each ET day is ONE observation (unweighted). Point = mean of per-day means; CI from
    resampling days with replacement — the honest small-sample interval."""
    arr = np.asarray(per_day_mean, dtype=float)
    n = len(arr)
    point = float(arr.mean())
    boot = np.array([arr[rng.integers(0, n, n)].mean() for _ in range(N_BOOT)])
    return point, float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))


def main() -> int:
    ap = argparse.ArgumentParser(description="Realized per-family maker toxicity from our fills")
    ap.add_argument("--by", choices=["family", "sport"], default="family")
    ap.add_argument("--fills", default=FILLS_CSV)
    args = ap.parse_args()

    # key -> et_day -> list[markout_c per fill]
    by_key: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    total = kept = 0
    with open(args.fills) as fh:
        for row in csv.DictReader(fh):
            total += 1
            mk = row.get("markout_c")
            if not mk or mk == "nan":
                continue
            try:
                val = float(mk)
            except ValueError:
                continue
            tk = row["market"]
            key = sport(tk) if args.by == "sport" else f"{sport(tk)}/{market_type(tk)}"
            by_key[key][_et_day(row["ts_utc"])].append(val)
            kept += 1

    rng = np.random.default_rng(7)
    rows: list[dict[str, object]] = []
    for key, days in by_key.items():
        day_means = [float(np.mean(v)) for v in days.values()]
        n_fills = sum(len(v) for v in days.values())
        n_days = len(days)
        max_day_share = max(len(v) for v in days.values()) / n_fills
        fill_wtd = float(np.average(
            [np.mean(v) for v in days.values()], weights=[len(v) for v in days.values()]))
        if n_days >= MIN_DAYS:
            point, lo, hi = dayblock_ci(day_means, rng)
            verdict = "TOXIC" if hi < 0 else ("BENIGN" if lo >= -0.05 else "INCONCLUSIVE")
        else:
            point, lo, hi, verdict = float(np.mean(day_means)), float("nan"), float("nan"), "INSUFF"
        rows.append({
            "family": key, "days": n_days, "fills": n_fills,
            "markout_day": point, "lo": lo, "hi": hi, "fill_wtd": fill_wtd,
            "concentrated": "!" if max_day_share > 0.40 else "", "verdict": verdict,
        })

    # sort most-toxic first; INSUFF families sink to the bottom
    rows.sort(key=lambda r: 99.0 if r["verdict"] == "INSUFF" else cast(float, r["markout_day"]))
    print(f"parsed {total} fills, {kept} with a markout, over {args.by} families\n")
    print("REALIZED MAKER TOXICITY — mean 30s fill markout per family (cents); <0 = picked off")
    print("day-block bootstrap 95% CI; PRIMARY = unweighted per-day mean (each day one obs)\n")
    hdr = f"{'family':<22}{'days':>5}{'fills':>7}{'markout':>9}{'95% CI':>17}{'fwtd':>8} verdict"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        ci = f"[{r['lo']:+.2f},{r['hi']:+.2f}]" if r["verdict"] != "INSUFF" else "(<3 days)"
        print(f"{r['family']:<22}{r['days']:>5}{r['fills']:>7}{r['markout_day']:>+9.3f}"
              f"{ci:>18}{r['fill_wtd']:>+9.3f}  {r['verdict']}{r['concentrated']}")

    print("\nSanity/instrument check: the KNOWN-TOXIC families (ITF, MLB/NBA GAME) must read")
    print("negative here — this is real money, so if they don't, the sign or data is wrong, not")
    print("the market benign. The pre-committed hypothesis (soccer TOTAL/SPREAD) reading ~0/+ is")
    print("the edge claim. '!' = one day holds >40% of fills, so the CI is effectively that day.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
