"""#5 신호 t → 체결 t+1 · #14 벤치마크 · #18 목표 조기 선언 · 워크포워드 · 학습 모드 리포트."""

from __future__ import annotations

import datetime as dt

import pytest

from app.backtest.costs import CostModel
from app.backtest.engine import PriceBook
from app.backtest.walkforward import expand_grid, make_folds
from app.data.pit_store import PITStore
from app.execution.broker import Order
from app.execution.paper_sim import PaperBroker


@pytest.fixture()
def broker(seeded):
    store = PITStore()
    end = dt.date(2020, 12, 31)
    book = PriceBook(store.prices(end, start=dt.date(2020, 1, 1)), store.fx(end, pair="USDKRW"))
    return PaperBroker(book, CostModel.from_config(), {"ACC_L": 100_000_000, "ACC_S": 1_000_000})


# ---------------------------------------------------------------- t → t+1
def test_same_day_fill_is_rejected(broker):
    """신호일과 체결일이 같으면 룩어헤드다. 무조건 거부."""
    d = dt.date(2020, 6, 15)
    o = Order("AAPL", "US", +1, 10, "s1", "ACC_L", signal_date=d)
    fills = broker.submit([o], d)
    assert fills[0].rejected and "t+1" in fills[0].reject_reason


def test_next_day_fill_uses_open(broker):
    sig, fill_day = dt.date(2020, 6, 15), dt.date(2020, 6, 16)
    o = Order("AAPL", "US", +1, 10, "s1", "ACC_L", signal_date=sig)
    fills = broker.submit([o], fill_day)
    assert not fills[0].rejected
    bar = broker.book.bar("AAPL", PriceBook.to_ord(fill_day))
    # 슬리피지 때문에 시가보다 약간 비싸게 체결된다.
    assert fills[0].price >= bar["o"]
    assert fills[0].price / bar["o"] - 1 < 0.01


def test_missing_bar_discards_signal(broker):
    """휴장일 체결 시도 → 신호 폐기."""
    o = Order("AAPL", "US", +1, 10, "s1", "ACC_L", signal_date=dt.date(2020, 12, 24))
    fills = broker.submit([o], dt.date(2020, 12, 25))     # 성탄절 휴장
    assert fills[0].rejected and "시가 결측" in fills[0].reject_reason


def test_short_position_records_negative_qty(broker):
    o = Order("MSFT", "US", -1, 5, "s_short", "ACC_L", signal_date=dt.date(2020, 6, 15),
              family="SHORT_US", horizon_days=10, stop_pct=0.08)
    broker.submit([o], dt.date(2020, 6, 16))
    pos = {p.symbol: p for p in broker.positions("ACC_L")}
    assert pos["MSFT"].qty < 0


def test_close_produces_realized_trade(broker):
    o = Order("AAPL", "US", +1, 10, "s1", "ACC_L", signal_date=dt.date(2020, 6, 15),
              family="LONG", horizon_days=5, stop_pct=0.08)
    broker.submit([o], dt.date(2020, 6, 16))
    trade = broker.close("ACC_L", "AAPL", 200.0, dt.date(2020, 6, 23), "horizon")
    assert trade is not None
    assert trade["closed"] == 1
    assert trade["exit_reason"] == "horizon"
    assert "pnl_pct" in trade
    assert not broker.positions("ACC_L")


def test_carry_accrues_for_shorts(broker):
    o = Order("MSFT", "US", -1, 5, "s", "ACC_L", signal_date=dt.date(2020, 6, 15),
              family="SHORT_US", horizon_days=10, stop_pct=0.08)
    broker.submit([o], dt.date(2020, 6, 16))
    before = broker.cash("ACC_L").amount
    total = broker.accrue_carry(dt.date(2020, 6, 17))
    assert total > 0, "공매도는 대차수수료가 붙어야 한다"
    assert broker.cash("ACC_L").amount < before


def test_broker_state_persists(broker, seeded):
    o = Order("AAPL", "US", +1, 10, "s1", "ACC_L", signal_date=dt.date(2020, 6, 15))
    broker.submit([o], dt.date(2020, 6, 16))
    reloaded = PaperBroker(broker.book, CostModel.from_config(),
                           {"ACC_L": 100_000_000, "ACC_S": 1_000_000})
    assert [p.symbol for p in reloaded.positions("ACC_L")] == ["AAPL"]


# ---------------------------------------------------------------- 워크포워드
def test_folds_are_time_ordered_and_non_overlapping_in_test():
    folds = make_folds(dt.date(2010, 1, 1), dt.date(2020, 12, 31),
                       train_months=36, test_months=6, step_months=6)
    assert len(folds) >= 10
    for f in folds:
        assert f.train_end < f.test_start <= f.test_end
    for a, b in zip(folds, folds[1:], strict=False):
        assert a.test_start < b.test_start


def test_grid_size_is_capped():
    with pytest.raises(ValueError, match="상한"):
        expand_grid({"a": list(range(10)), "b": list(range(10))}, 50)
    assert len(expand_grid({"a": [1, 2], "b": [3, 4]}, 50)) == 4


def test_walkforward_requires_min_folds(seeded):
    from app.backtest.walkforward import WalkForward
    from app.features.builder import FeatureBuilder

    store = PITStore()
    end = dt.date(2020, 12, 31)
    book = PriceBook(store.prices(end, start=dt.date(2019, 1, 1)))
    panel = FeatureBuilder(store).build(end, start=dt.date(2019, 1, 1))
    wf = WalkForward(panel, book, CostModel.from_config())
    from app.strategies.registry import get_strategy

    with pytest.raises(ValueError, match="폴드"):
        wf.run(get_strategy("h5_breakout_vol"), dt.date(2019, 1, 1), end)


# ---------------------------------------------------------------- #14 · #18 리포트
def _report(**over):
    from app.reports.daily import DailyReport

    base = {
        "date": dt.date(2020, 6, 30), "mode": "실시간",
        "accounts": {"ACC_L": {"nav": 1e8, "cash": 2e7, "capital": 1e8, "drawdown": -0.02,
                               "gross_notional_pct": 0.9, "short_notional_pct": 0.1,
                               "lev_inv_notional_pct": 0.05},
                     "ACC_S": {"nav": 1e6, "cash": 2e5, "capital": 1e6, "drawdown": -0.03,
                               "gross_notional_pct": 0.8, "short_notional_pct": 0.1,
                               "lev_inv_notional_pct": 0.1}},
        "performance": {"s1": {"n": 12, "win_rate": 0.47, "expectancy": -0.002, "ci": (0.24, 0.71)}},
        "integrity": {"summary": "전 항목 통과", "quarantined": []},
    }
    base.update(over)
    return DailyReport(**base)


def test_report_marks_small_sample_as_not_meaningful(sandbox):
    from app.reports.daily import render_daily

    body, _ = render_daily(_report())
    assert "표본이 적어" in body, "#18 n<30 을 결론처럼 쓰면 안 된다"
    assert "n < 30 은 참고용" in body
    assert "청산 100거래" in body


def test_report_always_shows_ci(sandbox):
    from app.reports.daily import render_daily

    body, _ = render_daily(_report())
    assert "95% CI" in body


def test_report_labels_short_simulation(sandbox):
    from app.reports.daily import render_daily

    body, _ = render_daily(_report(has_short=True))
    assert "실전 재현성 낮음" in body


def test_report_banners_synthetic_and_bypass(sandbox):
    from app.reports.daily import render_daily

    body, _ = render_daily(_report(synthetic=True, bypassed=True))
    assert "SYNTHETIC" in body and "가드 우회" in body


def test_telegram_summary_within_10_lines(sandbox):
    from app.reports.daily import render_daily

    _, telegram = render_daily(_report())
    assert len(telegram.splitlines()) <= 10


def test_glossary_explains_each_term_once(sandbox):
    from app.reports.daily import render_daily
    from app.reports.glossary import GlossaryTracker

    tracker = GlossaryTracker()
    body1, _ = render_daily(_report(), tracker=tracker)
    body2, _ = render_daily(_report(), tracker=tracker)
    terms1 = body1.split("## 오늘의 용어")[-1] if "## 오늘의 용어" in body1 else ""
    terms2 = body2.split("## 오늘의 용어")[-1] if "## 오늘의 용어" in body2 else ""
    assert terms1 and terms1 != terms2, "같은 용어를 반복 설명하고 있습니다 (§13.1)"


def test_interpretations_are_short(sandbox):
    from app.reports.glossary import interpret

    for metric, value in [("win_rate", 0.47), ("expectancy", -0.002), ("sharpe", 0.3),
                          ("mdd", 0.25), ("edge", -0.01)]:
        text = interpret(metric, value, n=100)
        assert text and len(text) <= 60, f"{metric} 해석이 너무 깁니다: {text}"


def test_monthly_report_blocks_early_conclusion(seeded):
    from app.reports.periodic import write_monthly

    p = write_monthly(dt.date(2020, 6, 30))
    body = p.read_text(encoding="utf-8")
    assert "공식 판정 불가" in body, "#18 100거래 전에 결론을 내면 안 된다"
    assert "K 는 리셋되지 않습니다" in body


def test_meta_review_has_all_sections(seeded):
    from app.reports.periodic import write_meta_review

    body = write_meta_review(dt.date(2020, 12, 31)).read_text(encoding="utf-8")
    for section in ("데이터", "게이트", "가설", "비용 모델", "유니버스", "결론"):
        assert f"## {section}" in body
