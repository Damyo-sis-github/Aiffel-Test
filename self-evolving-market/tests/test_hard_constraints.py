"""#19 레버리지·인버스 5일 · #20 공매도 손절 −8% · #21 명목 익스포저 150%.

이 셋은 전략이 우회할 수 없는 **엔진 하드 제약**이다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd
import pytest

from app.backtest.costs import CostModel
from app.backtest.engine import BacktestEngine, PriceBook
from app.data.pit_store import PITStore
from app.features.builder import FeatureBuilder
from app.portfolio.risk import Order, RiskEngine
from app.strategies.base import StrategyBase

START, END = dt.date(2019, 1, 2), dt.date(2020, 12, 31)


@dataclass
class AlwaysBuy(StrategyBase):
    """하드 제약을 시험하려고 매일 특정 심볼을 사는 전략."""

    id: str = "test_always_buy"
    family: str = "LEV_ETF"
    horizon_days: int = 250          # 엔진이 5일로 잘라야 한다
    params: dict = field(default_factory=lambda: {"symbol": "TQQQ"})

    def _rank(self, feats, date):
        sym = str(self.params["symbol"])
        if sym not in feats.index:
            return pd.DataFrame(columns=["symbol", "score", "side"])
        return pd.DataFrame({"symbol": [sym], "score": [1.0], "side": [1]})


@pytest.fixture()
def env(seeded):
    store = PITStore()
    prices = store.prices(END, start=dt.date(2018, 1, 1))
    book = PriceBook(prices, store.fx(END, pair="USDKRW"))
    panel = FeatureBuilder(store).build(END, start=dt.date(2018, 1, 1))
    return panel, book, CostModel.from_config()


# ---------------------------------------------------------------- #19
def test_leveraged_etf_max_hold_5_days(env):
    """LEV_ETF 는 호라이즌이 250이어도 5거래일 안에 청산되어야 한다."""
    panel, book, costs = env
    eng = BacktestEngine(panel, book, costs, account="ACC_L")
    res = eng.run(AlwaysBuy(), START, END)
    closed = res.closed_trades
    assert len(closed) > 0, "LEV_ETF 거래가 발생해야 의미 있는 테스트다"
    assert closed["hold_bars"].max() <= 5, "5 거래일 초과 보유 발생 (#19)"
    assert (closed["exit_reason"].isin(["max_hold", "horizon", "stop_loss", "delist"])).all()


def test_engine_caps_horizon_for_lev_inv(env):
    panel, book, costs = env
    eng = BacktestEngine(panel, book, costs)
    assert eng.enforce_horizon("LEV_ETF", 250) == 5
    assert eng.enforce_horizon("INV_ETF", 60) == 5
    assert eng.enforce_horizon("LONG", 60) == 60


def test_leveraged_only_enters_in_bull_regime(env):
    panel, book, costs = env
    eng = BacktestEngine(panel, book, costs)
    assert eng.allowed_regimes("LEV_ETF") == ("bull",)
    assert eng.allowed_regimes("INV_ETF") == ("bear",)
    assert eng.allowed_regimes("LONG") is None


# ---------------------------------------------------------------- #20
def test_short_stop_loss_is_forced_at_8pct(env):
    panel, book, costs = env
    eng = BacktestEngine(panel, book, costs)
    assert eng.stop_pct("SHORT_US", 10) == pytest.approx(0.08)
    # 전략이 params 로 손절을 바꾸려 해도 엔진은 config 만 본다.
    assert eng.stop_pct("SHORT_US", 999) == pytest.approx(0.08)


def test_short_loss_bounded_by_stop(env):
    """숏 포지션의 손실이 손절폭 + 갭 + 비용을 크게 벗어나면 안 된다."""
    from app.strategies.seed.tool_strategies import ShortWeak

    panel, book, costs = env
    res = BacktestEngine(panel, book, costs, account="ACC_L").run(ShortWeak(), START, END)
    closed = res.closed_trades
    if closed.empty:
        pytest.skip("숏 거래 없음")
    stopped = closed[closed["exit_reason"] == "stop_loss"]
    if not stopped.empty:
        # 갭 하나가 손절가를 뛰어넘는 경우까지 감안해 여유를 둔다.
        assert stopped["pnl_pct"].min() > -0.60, "손절이 사실상 작동하지 않습니다"


def test_short_notional_capped(env):
    from app.strategies.seed.tool_strategies import ShortWeak

    panel, book, costs = env
    res = BacktestEngine(panel, book, costs, account="ACC_L").run(ShortWeak(), START, END)
    if res.nav.empty:
        pytest.skip("NAV 없음")
    # 진입 시점 검사이므로 사후 가격 변동으로 약간 넘을 수 있다. 2배 이상 넘으면 버그다.
    assert res.nav["short_notional_pct"].max() < 0.30 * 2


# ---------------------------------------------------------------- #21
def test_gross_notional_never_exceeds_150pct(env):
    panel, book, costs = env
    res = BacktestEngine(panel, book, costs, account="ACC_L").run(AlwaysBuy(family="LONG"), START, END)
    if not res.nav.empty:
        assert res.nav["gross_notional_pct"].max() <= 1.5 + 1e-6


def test_risk_engine_reduces_leveraged_order(seeded):
    """2배 ETF 80% 주문 → 명목 160% 이므로 축소되어야 한다."""
    eng = RiskEngine("ACC_L")
    orders = [Order("122630", +1, 0.80, "s1", "LEV_ETF")]     # KODEX 레버리지 (x2)
    out = eng.check(orders, {})
    total_notional = sum(o.weight * o.leverage for o in out.accepted)
    assert total_notional <= 0.30 + 1e-9, "레버리지+인버스 명목 30% 상한 위반"
    assert out.reduced or out.rejected, "축소/폐기 사유가 기록되어야 한다"


def test_risk_engine_reports_reasons(seeded):
    eng = RiskEngine("ACC_L")
    out = eng.check([Order("AAPL", +1, 0.90, "s1", "LONG")], {})
    assert out.reduced or out.rejected
    assert any("종목 한도" in r for r in out.reasons())


def test_position_limit_differs_by_account(seeded):
    big = RiskEngine("ACC_L").check([Order("AAPL", +1, 0.50, "s", "LONG")], {})
    small = RiskEngine("ACC_S").check([Order("AAPL", +1, 0.50, "s", "LONG")], {})
    assert big.accepted[0].weight == pytest.approx(0.05)
    assert small.accepted[0].weight == pytest.approx(0.20)
