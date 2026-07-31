"""THE canonical ticker -> (sport, market_type) classifier — ONE source of truth.

Before this module the taxonomy lived in three places that had already drifted:
`realized_toxicity.py` (Python), `fct_toxicity_by_family.sql` (had club soccer), and
`fct_lp_market_session.sql` (did NOT — so club-soccer fills collapsed to sport='OTHER'
and never joined their verdict; the review-found silent no-op). This module is now the
single definition; both the Python bot/analysis AND the dbt marts derive from it:

  * Python callers import `sport()` / `market_type()`.
  * The dbt marts call `{{ classify_sport(col) }}` / `{{ classify_market_type(col) }}`,
    macros GENERATED from `SPORT_PREFIXES` / `MARKET_TYPE_TOKENS` here (`--emit-sql`), so
    a drift is a failing parity test, not a silent join miss.

Sport is a PREFIX match in order (first hit wins; KXWNBA is tried before KXNBA can't
collide since neither is a prefix of the other, but order still fixes any future overlap).
market_type is a SUBSTRING match (TOTAL/SPREAD live mid-ticker).

Usage:
    from ml.lp.classify import sport, market_type
    uv run python -m ml.lp.classify --emit-sql    # regenerate dbt/macros/classify.sql
"""

from __future__ import annotations

import argparse
import sys

# Ordered (canonical sport, matching ticker prefixes). Order is significant: first prefix
# hit wins, so the SQL CASE generated from this must preserve it. Soccer (WC + the club
# leagues) comes first because it is the pre-committed hypothesis and must never fall to OTHER.
SPORT_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("WC", ("KXWC",)),
    ("MLS", ("KXMLS",)),
    ("LIGAMX", ("KXLIGAMX",)),
    ("BRASILEIRO", ("KXBRASILEIRO",)),
    ("SCANDI", ("KXALLSVENSKAN", "KXELITESERIEN", "KXDENSUPERLIGA")),
    ("EPL", ("KXEPL",)),
    ("LALIGA", ("KXLALIGA",)),
    ("SERIEA", ("KXSERIEA",)),
    ("BUNDESLIGA", ("KXBUNDESLIGA",)),
    ("LIGUE1", ("KXLIGUE1",)),
    ("UCL_UEL", ("KXUCL", "KXUEL")),
    ("SOCCER_OTHER", ("KXEREDIVISIE", "KXEFLCHAMPIONSHIP", "KXCOPADOBRASIL", "KXSAUDIPL")),
    ("WNBA", ("KXWNBA",)),
    ("MLB", ("KXMLB",)),
    ("NCAA", ("KXNCAA",)),
    ("NBA", ("KXNBA",)),
    ("NHL", ("KXNHL",)),
    ("NFL", ("KXNFL",)),
    ("ATP", ("KXATP",)),
    ("ITF", ("KXITF",)),
    ("WTA", ("KXWTA",)),
)

# Ordered market-type substrings. TOTAL/SPREAD are the mean-reverting makeable types; the
# rest are directional and kept only so breakdowns can show why they lost.
MARKET_TYPE_TOKENS: tuple[str, ...] = ("TOTAL", "SPREAD", "MATCH", "WINNER", "GAME")

# Market BREADTH (Bartlett & O'Hara, "Adverse Selection in Prediction Markets: Evidence from
# Kalshi", 2026, 41.6M trades): one-sided TAKER-FLOW toxicity predicts maker losses in
# SINGLE_NAME markets (a specific named team/player's outcome) but NOT in BROAD ones (an
# aggregate/index outcome). So the flow-signed-markout axis only has power for SINGLE_NAME
# families; a one-sided flow reading on a BROAD family is not evidence of toxicity. Derived from
# market_type (TOTAL = the aggregate over/under; the rest name a side) so breadth can never
# disagree with the type it is coarsened from. Our own WC data already shows the pattern:
# WC/SPREAD (single-name) markout −0.135c significant vs WC/TOTAL (broad) −0.086c inconclusive.
BROAD_TYPES: frozenset[str] = frozenset({"TOTAL"})
SINGLE_NAME_TYPES: frozenset[str] = frozenset({"SPREAD", "MATCH", "WINNER", "GAME"})

# The soccer sports (used by callers deciding the post-WC club hypothesis universe).
SOCCER_SPORTS: frozenset[str] = frozenset({
    "WC", "MLS", "LIGAMX", "BRASILEIRO", "SCANDI", "EPL", "LALIGA", "SERIEA",
    "BUNDESLIGA", "LIGUE1", "UCL_UEL", "SOCCER_OTHER",
})


def sport(ticker: str) -> str:
    """Canonical sport family from a market ticker (prefix match, first hit wins)."""
    t = ticker.upper()
    for name, prefixes in SPORT_PREFIXES:
        if any(t.startswith(p) for p in prefixes):
            return name
    return "OTHER"


def market_type(ticker: str) -> str:
    """Canonical market structure (TOTAL / SPREAD / MATCH / WINNER / GAME / OTHER)."""
    t = ticker.upper()
    for token in MARKET_TYPE_TOKENS:
        if token in t:
            return token
    return "OTHER"


def breadth(ticker: str) -> str:
    """Market breadth — BROAD (aggregate outcome, e.g. TOTAL over/under) vs SINGLE_NAME (a
    specific named side, e.g. SPREAD/MATCH/WINNER/GAME) vs OTHER. Coarsened from market_type
    (see BROAD_TYPES / SINGLE_NAME_TYPES): the flow-signed-markout toxicity axis only has
    discriminating power for SINGLE_NAME families."""
    mt = market_type(ticker)
    if mt in BROAD_TYPES:
        return "BROAD"
    if mt in SINGLE_NAME_TYPES:
        return "SINGLE_NAME"
    return "OTHER"


def family(ticker: str) -> str:
    """The 'SPORT/TYPE' key the verdict marts + quotable gate join on."""
    return f"{sport(ticker)}/{market_type(ticker)}"


def _case(col: str, branches: list[tuple[str, str]]) -> str:
    """Render a SQL CASE. `branches` = list of (predicate_sql, result_label)."""
    lines = [f"        when {pred} then '{label}'" for pred, label in branches]
    return "    case\n" + "\n".join(lines) + "\n        else 'OTHER'\n    end"


def emit_sql() -> str:
    """Generate the exact contents of dbt/macros/classify.sql from the tables above.

    Keeping the macro a GENERATED artifact (guarded by a parity test) is what makes this
    module the single source of truth across Python and SQL."""
    sport_branches = [
        (" or ".join(f"{{{{ col }}}} like '{p}%'" for p in prefixes), name)
        for name, prefixes in SPORT_PREFIXES
    ]
    type_branches = [(f"{{{{ col }}}} like '%{tok}%'", tok) for tok in MARKET_TYPE_TOKENS]
    # Breadth reuses the market_type tokens IN THE SAME ORDER, mapping each to BROAD/SINGLE_NAME,
    # so classify_breadth picks the same token classify_market_type does -> parity with breadth().
    breadth_branches = [
        (f"{{{{ col }}}} like '%{tok}%'", "BROAD" if tok in BROAD_TYPES else "SINGLE_NAME")
        for tok in MARKET_TYPE_TOKENS
    ]
    return (
        "-- GENERATED by `python -m ml.lp.classify --emit-sql` from ml/lp/classify.py.\n"
        "-- Do NOT hand-edit: edit SPORT_PREFIXES / MARKET_TYPE_TOKENS there and regenerate.\n"
        "-- The canonical ticker -> (sport, market_type, breadth) taxonomy, shared by every mart\n"
        "-- so the fills side and the capture side classify identically and their verdicts join.\n"
        "\n"
        "{% macro classify_sport(col) %}\n"
        f"{_case('col', sport_branches)}\n"
        "{% endmacro %}\n"
        "\n"
        "{% macro classify_market_type(col) %}\n"
        f"{_case('col', type_branches)}\n"
        "{% endmacro %}\n"
        "\n"
        "{% macro classify_breadth(col) %}\n"
        f"{_case('col', breadth_branches)}\n"
        "{% endmacro %}\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Canonical ticker classifier / dbt macro codegen")
    ap.add_argument("--emit-sql", action="store_true", help="print the dbt macro to stdout")
    args = ap.parse_args()
    if args.emit_sql:
        print(emit_sql(), end="")
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
