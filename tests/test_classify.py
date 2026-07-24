"""Parity + contract tests for the canonical classifier (`ml.lp.classify`).

Two guarantees:
  1. The committed dbt macro is BYTE-IDENTICAL to what `emit_sql()` generates — so the SQL
     the marts use can never silently drift from the Python the bot uses (the review's bug).
  2. The Python classifier matches a fixed ticker->family fixture — the shared contract both
     sides are pinned to. Club soccer MUST classify (never OTHER).
"""

from __future__ import annotations

from pathlib import Path

from ml.lp.classify import SOCCER_SPORTS, emit_sql, family, market_type, sport

MACRO_FILE = Path(__file__).resolve().parents[1] / "dbt" / "macros" / "classify.sql"

# The contract: representative real tickers -> (sport, market_type).
FIXTURE: list[tuple[str, str, str]] = [
    ("KXWCSPREAD-26JUL19ARGFRA", "WC", "SPREAD"),
    ("KXWCTOTAL-26JUL19ARGFRA", "WC", "TOTAL"),
    ("KXMLSSPREAD-26JUL24LAFCSEA", "MLS", "SPREAD"),
    ("KXLIGAMXTOTAL-26JUL24AMECRU", "LIGAMX", "TOTAL"),
    ("KXBRASILEIROSPREAD-26JUL24FLAPAL", "BRASILEIRO", "SPREAD"),
    ("KXALLSVENSKANTOTAL-26JUL24", "SCANDI", "TOTAL"),
    ("KXELITESERIENSPREAD-26JUL24", "SCANDI", "SPREAD"),
    ("KXDENSUPERLIGATOTAL-26JUL24", "SCANDI", "TOTAL"),
    ("KXEPLTOTAL-26AUG16", "EPL", "TOTAL"),
    ("KXUCLSPREAD-26SEP17", "UCL_UEL", "SPREAD"),
    ("KXUELTOTAL-26SEP18", "UCL_UEL", "TOTAL"),
    ("KXEREDIVISIETOTAL-26AUG09", "SOCCER_OTHER", "TOTAL"),
    ("KXWNBATOTAL-26JUL24NYLLAS", "WNBA", "TOTAL"),
    ("KXNBATOTAL-26JUN10", "NBA", "TOTAL"),  # WNBA vs NBA must not collide
    ("KXMLBRFI-26JUN161915SFATL", "MLB", "OTHER"),  # RFI prop = no known type token
    ("KXATPMATCH-26JUL24", "ATP", "MATCH"),
    ("KXITFMATCH-26JUL24", "ITF", "MATCH"),
    ("KXWTAWINNER-26JUL24", "WTA", "WINNER"),
    ("KXNHLGAME-26OCT01", "NHL", "GAME"),
    ("KXNCAAFGAME-26AUG30", "NCAA", "GAME"),
    ("KXBTCD-26JUL24", "OTHER", "OTHER"),  # non-sport universe -> OTHER
]


def test_macro_file_matches_emit_sql() -> None:
    assert MACRO_FILE.read_text() == emit_sql(), (
        "dbt/macros/classify.sql is stale — regenerate with "
        "`uv run python -m ml.lp.classify --emit-sql > dbt/macros/classify.sql`"
    )


def test_python_classifier_matches_fixture() -> None:
    for ticker, exp_sport, exp_type in FIXTURE:
        assert sport(ticker) == exp_sport, ticker
        assert market_type(ticker) == exp_type, ticker
        assert family(ticker) == f"{exp_sport}/{exp_type}"


def test_all_club_soccer_is_soccer() -> None:
    # the post-WC hypothesis families must be recognized as soccer (drove the join bug).
    for ticker, exp_sport, _ in FIXTURE:
        if exp_sport in {"MLS", "LIGAMX", "BRASILEIRO", "SCANDI", "UCL_UEL", "SOCCER_OTHER"}:
            assert exp_sport in SOCCER_SPORTS
            assert sport(ticker) in SOCCER_SPORTS
