"""Offline unit tests for the WS microstructure feature engineering (ml.lp.ws_features).
No network: construct LocalBooks / feed trades+mids directly and check the math + windowing."""

from __future__ import annotations

from decimal import Decimal

from ingestion.kalshi_ws import LocalBook
from ml.lp.ws_features import MarketFeatureState, book_features


def _book(yes: dict[str, str], no: dict[str, str]) -> LocalBook:
    b = LocalBook()
    b.yes = {p: Decimal(s) for p, s in yes.items()}
    b.no = {p: Decimal(s) for p, s in no.items()}
    return b


def test_book_features_two_sided() -> None:
    bf = book_features(_book({"0.40": "100", "0.38": "50"}, {"0.55": "30", "0.53": "20"}))
    assert bf is not None
    assert bf["bid"] == 0.40 and bf["ask"] == 0.45  # ask = 1 - best no (0.55)
    assert round(bf["mid"], 4) == 0.425 and round(bf["spread_c"], 4) == 5.0
    assert bf["yes_depth"] == 150 and bf["no_depth"] == 50
    assert bf["imbalance"] == 0.5  # (150-50)/200 -> yes-heavy book
    assert bf["depth_near"] == 200 and bf["n_levels"] == 4


def test_book_features_one_sided_is_none() -> None:
    assert book_features(_book({"0.40": "100"}, {})) is None
    assert book_features(_book({}, {"0.55": "30"})) is None


def test_imbalance_sign_and_bounds() -> None:
    ask_heavy = book_features(_book({"0.40": "10"}, {"0.55": "90"}))
    assert ask_heavy is not None and ask_heavy["imbalance"] < 0  # more no-depth -> negative
    assert -1.0 <= ask_heavy["imbalance"] <= 1.0


def test_flow_features() -> None:
    st = MarketFeatureState(window_s=60)
    st.add_trades([(1000.0, 10, "yes"), (1000.0, 5, "no"), (1000.0, 5, "yes")], now=1000.0)
    f = st.flow_features()
    assert f["trades_1m"] == 3 and f["vol_1m"] == 20
    assert f["taker_buy_frac"] == 0.75  # 15 of 20 volume bought yes
    assert f["signed_flow_1m"] == 10  # +10 -5 +5


def test_window_eviction() -> None:
    st = MarketFeatureState(window_s=60)
    st.add_trades([(900.0, 10, "yes")], now=1000.0)  # 100s old > 60s window -> evicted
    f = st.flow_features()
    assert f["trades_1m"] == 0 and f["vol_1m"] == 0
    assert f["taker_buy_frac"] == 0.5  # no volume -> neutral fallback


def test_vol_features() -> None:
    st = MarketFeatureState(window_s=60)
    st.add_mid(0.40, now=1000.0)
    st.add_mid(0.44, now=1001.0)
    v = st.vol_features()
    assert round(v["midvol_1m"], 4) == 2.0  # pstdev([.40,.44]) = .02 -> 2.0c
    assert round(v["midmove_1m"], 4) == 4.0  # (.44-.40)*100


def test_vol_features_single_point_is_zero() -> None:
    st = MarketFeatureState(window_s=60)
    st.add_mid(0.40, now=1000.0)
    v = st.vol_features()
    assert v["midvol_1m"] == 0.0 and v["midmove_1m"] == 0.0
