"""Unit tests for the Step 0 breakeven math (`ml.lp.breakeven`).

The go/no-go for club soccer rests on these pure functions, so they get guarded:
the OLS line, the bootstrap CI at an arbitrary spread (incl. beyond the fitted grid —
the bug the first cut had), and breakeven-spread recovery on a synthetic linear world.
"""

from __future__ import annotations

import numpy as np

from ml.lp.breakeven import _wls_line, fit_family, predict_net
from ml.lp.realized_toxicity import market_type, sport


def test_wls_line_recovers_known_slope() -> None:
    x = np.array([1.0, 2.0, 3.0, 4.0])
    y = 0.3 * x - 0.2  # a=-0.2, b=0.3
    a, b = _wls_line(x, y)
    assert abs(a - (-0.2)) < 1e-9
    assert abs(b - 0.3) < 1e-9


def test_wls_line_zero_variance_is_nan() -> None:
    a, b = _wls_line(np.array([2.0, 2.0, 2.0]), np.array([1.0, 2.0, 3.0]))
    assert np.isnan(a) and np.isnan(b)


def test_predict_net_point_and_ci_beyond_grid() -> None:
    # a fit with a tight bootstrap cloud around a=-0.2, b=0.3
    fit = {
        "a": -0.2, "b": 0.3,
        "boot_a": np.full(200, -0.2), "boot_b": np.full(200, 0.3),
    }
    # at 8c — well beyond the [1,4] reference grid — the CI must still bracket the point
    point, lo, hi = predict_net(fit, 8.0)
    assert abs(point - (0.3 * 8.0 - 0.2)) < 1e-9
    assert lo <= point <= hi


def test_fit_family_recovers_breakeven_spread() -> None:
    # synthetic soccer-like world: net = 0.3*spread - 0.2  -> breakeven at 0.667c.
    # capture and markout are split so cap+mk == net; spreads vary within and across days.
    rng = np.random.default_rng(0)
    days: dict[str, list[tuple[float, float, float]]] = {}
    for d in range(8):
        pts = []
        for _ in range(60):
            s = float(rng.uniform(1.0, 5.0))
            net = 0.3 * s - 0.2
            pts.append((s, net + 0.05, -0.05))  # capture, markout -> sum = net
        days[f"2026-06-{d + 1:02d}"] = pts
    fit = fit_family(days, np.random.default_rng(7))
    assert abs(fit["b"] - 0.3) < 0.03
    assert abs(fit["star"] - (0.2 / 0.3)) < 0.15  # breakeven ~0.667c
    lo, hi = fit["star_ci"]
    assert lo <= fit["star"] <= hi


def test_club_soccer_classifies_not_other() -> None:
    # the review-found mart bug: club leagues must map to a soccer family, never OTHER.
    for tk, exp in [
        ("KXMLSSPREAD-25JUL24", "MLS"),
        ("KXLIGAMXTOTAL-25JUL24", "LIGAMX"),
        ("KXBRASILEIROSPREAD-25JUL24", "BRASILEIRO"),
    ]:
        assert sport(tk) == exp
    assert market_type("KXMLSSPREAD-25JUL24") == "SPREAD"
    assert market_type("KXLIGAMXTOTAL-25JUL24") == "TOTAL"
