"""Fail-CLOSED quotable-family policy — the gate that decides what the live bot may quote.

Before this, the bot quoted any ticker whose prefix was in a hardcoded prefix list
that still included families we had MEASURED toxic (MLB/WNBA/ITF), and NOTHING reconciled it
against the edge verdict — a fail-OPEN loop the review flagged. Now the bot reads
`quotable_families.json` (produced by `edge_verdict --emit`) and quotes ONLY families that are
freshly CONFIRMED (flow-benign AND our realized capture > 0). Everything else is refused:

  * a missing / STALE file (capture pipeline died) -> refuse everything (idle),
  * an unrecognized or merely-CANDIDATE family     -> refuse,
  * an explicit `--pilot KXLIGAMX` prefix           -> the ONE audited way to quote an
    unconfirmed family, to gather its first realized evidence under hard caps.

`allows()` is consulted inside `lp_gate.passes_gate`, so BOTH selection paths
(pick_smooth_ticker and better_market) enforce it with one hook. Default policy = None
(allow all), so the paper simulator and the historical re-score are unaffected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from core.maker.classify import family


@dataclass(frozen=True)
class Quotable:
    """An immutable snapshot of what may be quoted right now."""

    confirmed: frozenset[str]  # families the bot may quote at full size (fresh + realized+)
    candidates: frozenset[str]  # flow-benign but never traded — pilot-only, informational
    as_of_day: str | None  # freshness anchor = max capture_day behind the verdict
    stale: bool  # True if the file is missing or older than the staleness bound
    pilot_prefixes: tuple[str, ...]  # explicit --pilot overrides (upper-cased ticker prefixes)
    source: str  # human-readable provenance for the startup banner

    def _is_pilot(self, ticker: str) -> bool:
        t = ticker.upper()
        return any(t.startswith(p) for p in self.pilot_prefixes)

    def allows(self, ticker: str) -> bool:
        """True iff this ticker's family may be quoted: an explicit pilot, OR a fresh
        CONFIRMED family. Stale/missing verdict => only pilots pass (fail-closed)."""
        if self._is_pilot(ticker):
            return True
        return (not self.stale) and family(ticker) in self.confirmed

    def reason(self, ticker: str) -> str:
        """Why a ticker is allowed / refused — for the startup banner and debug logs."""
        fam = family(ticker)
        if self._is_pilot(ticker):
            return f"PILOT ({fam})"
        if self.stale:
            return f"REFUSED (verdict {'missing' if self.as_of_day is None else 'STALE'})"
        if fam in self.confirmed:
            return f"CONFIRMED ({fam})"
        if fam in self.candidates:
            return f"REFUSED ({fam} is CANDIDATE — needs --pilot to gather evidence)"
        return f"REFUSED ({fam} not confirmed)"


def load_quotable(
    path: str,
    today: date,
    max_stale_days: int = 3,
    pilot_prefixes: tuple[str, ...] = (),
) -> Quotable:
    """Load the verdict file into a policy. A missing file or an as-of day older than
    `max_stale_days` marks the policy STALE (confirmed families refused; pilots still pass)."""
    pilots = tuple(p.strip().upper() for p in pilot_prefixes if p.strip())
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return Quotable(frozenset(), frozenset(), None, True, pilots, f"{path} (MISSING)")

    confirmed = frozenset(str(f) for f in doc.get("quotable", []))
    fams = doc.get("families", {})
    candidates = frozenset(f for f, v in fams.items() if v.get("tier") == "CANDIDATE")
    as_of = doc.get("as_of_capture_day")
    stale = True
    if as_of:
        try:
            stale = (today - date.fromisoformat(str(as_of))).days > max_stale_days
        except ValueError:
            stale = True
    src = f"{path} (as-of {as_of}, {'STALE' if stale else 'fresh'})"
    return Quotable(confirmed, candidates, as_of, stale, pilots, src)
