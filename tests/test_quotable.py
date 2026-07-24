"""Tests for the fail-CLOSED quotable-family loop: edge_verdict tiering + emit, the Quotable
policy (freshness / pilot / round-trip), and its enforcement in lp_gate.passes_gate."""

from __future__ import annotations

from datetime import date

import pytest

from ml.lp import lp_gate
from ml.lp.edge_verdict import emit_quotable, tier_for
from ml.lp.quotable import load_quotable

ROWS: list[dict[str, object]] = [
    {"family": "WC/SPREAD", "verdict": "FLOW-BENIGN", "tier": "CONFIRMED", "markout_c": -0.05,
     "ci_tuple": [-0.10, 0.02], "days": 8, "flow_obs": 1200, "capture_usd": 61.0,
     "realized_markout_c": -0.13},
    {"family": "MLS/SPREAD", "verdict": "FLOW-BENIGN", "tier": "CANDIDATE", "markout_c": 0.0,
     "ci_tuple": [-0.10, 0.08], "days": 6, "flow_obs": 800, "capture_usd": None,
     "realized_markout_c": None},
    {"family": "MLB/TOTAL", "verdict": "FLOW-TOXIC", "tier": None, "markout_c": 0.5,
     "ci_tuple": [0.2, 0.8], "days": 8, "flow_obs": 1000, "capture_usd": -30.0,
     "realized_markout_c": -0.69},
]


def test_tier_for() -> None:
    assert tier_for("FLOW-BENIGN", 61.0) == "CONFIRMED"
    assert tier_for("FLOW-BENIGN", None) == "CANDIDATE"
    assert tier_for("FLOW-BENIGN", -5.0) == "CONTRADICTION"  # benign flow but our fills bled
    assert tier_for("FLOW-TOXIC", 10.0) is None
    assert tier_for("INCONCLUSIVE", None) is None


def _emit(tmp_path: object) -> str:
    path = str(tmp_path / "quotable_families.json")  # type: ignore[operator]
    meta = {"min_days": 5, "min_flow_obs": 500, "benign_ci_hi": 0.10}
    quotable = emit_quotable(ROWS, "2026-07-24", meta, path)
    assert quotable == ["WC/SPREAD"]  # only the CONFIRMED family
    return path


def test_emit_then_load_roundtrip(tmp_path: object) -> None:
    path = _emit(tmp_path)
    pol = load_quotable(path, date(2026, 7, 25), max_stale_days=3)
    assert not pol.stale
    assert pol.confirmed == frozenset({"WC/SPREAD"})
    assert pol.candidates == frozenset({"MLS/SPREAD"})  # toxic MLB/TOTAL omitted entirely
    assert pol.allows("KXWCSPREAD-26JUL24")           # confirmed -> quotable
    assert not pol.allows("KXMLSSPREAD-26JUL24")       # candidate -> refused without pilot
    assert not pol.allows("KXMLBTOTAL-26JUL24")        # toxic -> absent -> refused


def test_stale_file_refuses_confirmed(tmp_path: object) -> None:
    path = _emit(tmp_path)
    pol = load_quotable(path, date(2026, 8, 1), max_stale_days=3)  # as-of 07-24 -> 8 days old
    assert pol.stale
    assert not pol.allows("KXWCSPREAD-26JUL24")  # fresh CONFIRMED requirement fails when stale


def test_missing_file_is_failclosed_but_pilot_passes() -> None:
    pol = load_quotable("/nonexistent/quotable_families.json", date(2026, 7, 25),
                        pilot_prefixes=("KXLIGAMX",))
    assert pol.stale and not pol.confirmed
    assert not pol.allows("KXWCSPREAD-26JUL24")     # nothing confirmed
    assert pol.allows("KXLIGAMXTOTAL-26JUL24")      # explicit pilot still passes


def test_pilot_overrides_candidate(tmp_path: object) -> None:
    path = _emit(tmp_path)
    pol = load_quotable(path, date(2026, 7, 25), pilot_prefixes=("KXMLS",))
    assert pol.allows("KXMLSSPREAD-26JUL24")  # pilot lets the unconfirmed family through
    assert "PILOT" in pol.reason("KXMLSSPREAD-26JUL24")


def test_passes_gate_enforces_policy(tmp_path: object) -> None:
    path = _emit(tmp_path)
    pol = load_quotable(path, date(2026, 7, 25))
    try:
        lp_gate.QUOTABLE_POLICY = pol
        assert lp_gate.passes_gate("KXWCSPREAD-26JUL24", 100)      # confirmed + flow ok
        assert not lp_gate.passes_gate("KXMLSSPREAD-26JUL24", 100)  # candidate -> refused
        assert not lp_gate.passes_gate("KXWCSPREAD-26JUL24", 3)     # confirmed but too thin
        assert not lp_gate.passes_gate("KXITFMATCH-26JUL24", 100)   # toxic type still excluded
    finally:
        lp_gate.QUOTABLE_POLICY = None  # never leak the policy into other tests


def test_passes_gate_default_allows_all() -> None:
    # with no policy installed (paper sim / re-score), the family gate is a no-op.
    assert lp_gate.QUOTABLE_POLICY is None
    assert lp_gate.passes_gate("KXMLSSPREAD-26JUL24", 100)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
