"""Persistent DAILY loss budget — the cap that survives across sessions and processes.

The per-market and per-session kill switches bound ONE run. They say nothing about a
scheduler firing six sessions a night, or about a human relaunching after a bad one. Those
are exactly the paths that let a small, capped strategy bleed steadily — so the budget lives
in a FILE, not in a process.

Design notes:
  * Keyed by ET day (the repo's trading-day convention, and the unit our bootstraps resample
    on) so a session spanning midnight UTC does not silently get a fresh budget mid-run.
  * Records REALIZED P&L only, appended as each market closes — never an open mark, so a
    transient drawdown cannot exhaust the day.
  * Thread-safe: `live_multi` records from several worker threads at once.
  * FAIL-CLOSED on a corrupt/unreadable file: treat the day as exhausted rather than assume
    room. A budget that fails open is not a budget.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

BUDGET_FILE = "data/daily_budget.json"
# Dollars of REALIZED loss allowed per ET day, across every session and process.
DAILY_LOSS_BUDGET = 5.0

_LOCK = threading.Lock()


def et_day(now: datetime | None = None) -> str:
    """Today in ET (UTC-4). Matches the day unit used by the toxicity bootstraps.
    Accepts naive or aware datetimes; naive is treated as UTC."""
    t = now or datetime.now(UTC)
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return (t - timedelta(hours=4)).strftime("%Y-%m-%d")


def _read(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data: dict[str, Any] = json.loads(p.read_text())
        return data
    except Exception:
        # Unreadable -> treat as exhausted (see module docstring): a budget that fails open
        # is not a budget. Signalled with a sentinel the callers below understand.
        return {"day": et_day(), "realized": -abs(DAILY_LOSS_BUDGET), "corrupt": True}


def realized_today(path: str = BUDGET_FILE) -> float:
    """Realized P&L booked so far today (negative = losses)."""
    d = _read(path)
    if d.get("day") != et_day():
        return 0.0  # a new ET day resets the budget
    return float(d.get("realized") or 0.0)


def remaining(path: str = BUDGET_FILE, budget: float = DAILY_LOSS_BUDGET) -> float:
    """Dollars of loss still permitted today (0.0 once exhausted)."""
    return max(0.0, budget + min(0.0, realized_today(path)))


def exhausted(path: str = BUDGET_FILE, budget: float = DAILY_LOSS_BUDGET) -> bool:
    return remaining(path, budget) <= 0.0


def record(pnl: float, path: str = BUDGET_FILE) -> float:
    """Book a market's REALIZED P&L against today and return the new day total.
    Thread-safe; called as each market closes."""
    with _LOCK:
        d = _read(path)
        total = (float(d.get("realized") or 0.0) if d.get("day") == et_day() else 0.0) + pnl
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"day": et_day(), "realized": round(total, 4),
             "sessions": int(d.get("sessions") or 0) + (1 if d.get("day") == et_day() else 1)},
            indent=2) + "\n")
        return total


def status_line(path: str = BUDGET_FILE, budget: float = DAILY_LOSS_BUDGET) -> str:
    used = realized_today(path)
    return (f"daily budget: ${used:+.2f} realized today, "
            f"${remaining(path, budget):.2f} of ${budget:.2f} remaining ({et_day()} ET)")
