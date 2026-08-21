"""Unit tests for the shared money math in ml/backtest.py.

Every PnL number in the project flows through `_fee`, `_per_window_pnl`,
`_summarise`, and `_effective_quote` — ~10 analysis scripts import them. These
tests pin the Kalshi fee formula, the accounting identities, and the quote
arithmetic so a silent regression can't quietly invalidate every reported ROI.
"""

import numpy as np
import pytest

from core.backtest.backtest import (
    _breakeven_cost,
    _effective_quote,
    _fee,
    _per_window_pnl,
    _summarise,
)


def arr(*vals: float) -> np.ndarray:
    return np.asarray(vals, dtype=float)


class TestFee:
    """Kalshi trade fee = ceil_to_cent(0.07 * P * (1-P))."""

    def test_known_values(self) -> None:
        # 0.07 * 0.5 * 0.5 = 0.0175 -> ceil to 0.02
        # 0.07 * 0.9 * 0.1 = 0.0063 -> ceil to 0.01
        # 0.07 * 0.99 * 0.01 ~ 0.0007 -> ceil to 0.01 (never free while 0 < P < 1)
        np.testing.assert_allclose(_fee(arr(0.5, 0.9, 0.99)), arr(0.02, 0.01, 0.01))

    def test_boundaries_are_free(self) -> None:
        np.testing.assert_allclose(_fee(arr(0.0, 1.0)), arr(0.0, 0.0))

    def test_symmetric_in_price(self) -> None:
        p = np.linspace(0.01, 0.99, 99)
        np.testing.assert_allclose(_fee(p), _fee(1.0 - p))


class TestPerWindowPnl:
    def test_no_bet_means_no_money(self) -> None:
        pnl, stake = _per_window_pnl(
            arr(0).astype(bool), arr(0).astype(bool), np.array([1]), arr(0.6), arr(0.5)
        )
        assert pnl[0] == 0.0
        assert stake[0] == 0.0

    def test_win_and_loss_accounting(self) -> None:
        # One YES bet at ask 0.60 (fee 0.02): win -> 1 - 0.62, lose -> -0.62.
        bet_yes = np.array([True, True])
        bet_no = np.array([False, False])
        outcome = np.array([1, 0])
        pnl, stake = _per_window_pnl(bet_yes, bet_no, outcome, arr(0.6, 0.6), arr(0.5, 0.5))
        np.testing.assert_allclose(pnl, arr(1.0 - 0.62, -0.62))
        np.testing.assert_allclose(stake, arr(0.62, 0.62))

    def test_settlement_identity(self) -> None:
        """For every betting window, pnl + stake == the $1-or-$0 settlement payout —
        the accounting identity that catches sign/fee bookkeeping errors."""
        rng = np.random.default_rng(0)
        n = 500
        yes_ask = rng.uniform(0.02, 0.98, n)
        no_ask = rng.uniform(0.02, 0.98, n)
        outcome = rng.integers(0, 2, n)
        side = rng.integers(0, 3, n)  # 0 = yes, 1 = no, 2 = no bet
        bet_yes, bet_no = side == 0, side == 1
        pnl, stake = _per_window_pnl(bet_yes, bet_no, outcome, yes_ask, no_ask)

        payout = np.where(bet_yes, outcome == 1, np.where(bet_no, outcome == 0, 0.0)).astype(float)
        np.testing.assert_allclose(pnl + stake, payout, atol=1e-12)
        assert (stake[bet_yes | bet_no] > 0).all()
        np.testing.assert_allclose(pnl[~(bet_yes | bet_no)], 0.0)


class TestSummarise:
    def test_aggregates_per_window_pnl(self) -> None:
        """_summarise must be exactly the aggregation of _per_window_pnl — the
        contract that lets the bootstrap scripts share the same money math."""
        rng = np.random.default_rng(1)
        n = 300
        yes_ask = rng.uniform(0.05, 0.95, n)
        no_ask = 1.0 - yes_ask + rng.uniform(0.0, 0.02, n)
        outcome = rng.integers(0, 2, n)
        prob = rng.uniform(0, 1, n)
        bet_yes, bet_no = (prob - yes_ask) > 0, ((1.0 - no_ask) - prob) > 0

        r = _summarise(bet_yes, bet_no, outcome, yes_ask, no_ask)
        pnl, stake = _per_window_pnl(bet_yes, bet_no, outcome, yes_ask, no_ask)
        assert r["n_bets"] == float((bet_yes | bet_no).sum())
        assert r["pnl"] == pytest.approx(pnl.sum())
        assert r["roi"] == pytest.approx(pnl.sum() / stake.sum())

    def test_hand_computed_example(self) -> None:
        # YES wins at 0.40 (fee 0.02): +0.58. NO loses at 0.30 (fee 0.02): -0.32.
        r = _summarise(
            np.array([True, False]),
            np.array([False, True]),
            np.array([1, 1]),
            arr(0.40, 0.70),
            arr(0.65, 0.30),
        )
        assert r["n_bets"] == 2.0
        assert r["win_rate"] == 0.5
        assert r["pnl"] == pytest.approx(0.58 - 0.32)
        assert r["roi"] == pytest.approx((0.58 - 0.32) / (0.42 + 0.32))

    def test_no_bets_is_nan_not_crash(self) -> None:
        none = np.array([False, False])
        r = _summarise(none, none, np.array([1, 0]), arr(0.5, 0.5), arr(0.5, 0.5))
        assert r["n_bets"] == 0.0
        assert np.isnan(r["win_rate"])
        assert np.isnan(r["roi"])


class TestEffectiveQuote:
    def test_widens_symmetrically_and_clips(self) -> None:
        mid, spread = arr(0.50, 0.99), arr(0.02, 0.02)
        yes_ask, yes_bid, no_ask = _effective_quote(mid, spread, mult=2.0, slippage=0.01)
        # half = 0.02/2 * 2 + 0.01 = 0.03
        np.testing.assert_allclose(yes_ask, arr(0.53, 1.0))  # 1.02 clipped to 1.0
        np.testing.assert_allclose(yes_bid, arr(0.47, 0.96))
        np.testing.assert_allclose(no_ask, 1.0 - yes_bid)

    def test_recorded_quote_is_identity(self) -> None:
        mid, spread = arr(0.62), arr(0.04)
        yes_ask, yes_bid, _ = _effective_quote(mid, spread, mult=1.0, slippage=0.0)
        assert yes_ask[0] == pytest.approx(0.64)
        assert yes_bid[0] == pytest.approx(0.60)


class TestBreakevenCost:
    def test_dead_strategy_breaks_even_immediately(self) -> None:
        # prob == mid everywhere -> never clears the ask -> pnl 0 at zero extra cost.
        n = 50
        mid = np.full(n, 0.5)
        spread = np.full(n, 0.02)
        outcome = np.zeros(n, dtype=np.intp)
        assert _breakeven_cost(mid.copy(), outcome, mid, spread) == 0.0

    def test_perfect_foresight_survives_more_cost_than_noise(self) -> None:
        """An oracle (prob = outcome) must have a strictly larger breakeven than an
        anti-oracle — the monotonicity that makes the breakeven number meaningful."""
        rng = np.random.default_rng(2)
        n = 400
        outcome = rng.integers(0, 2, n).astype(np.intp)
        mid = np.clip(0.5 + (outcome - 0.5) * rng.uniform(0, 0.3, n), 0.05, 0.95)
        spread = np.full(n, 0.02)
        oracle = outcome.astype(float)
        anti = 1.0 - oracle
        be_oracle = _breakeven_cost(oracle, outcome, mid, spread)
        be_anti = _breakeven_cost(anti, outcome, mid, spread)
        assert be_anti == 0.0
        assert np.isnan(be_oracle) or be_oracle > 0.05
