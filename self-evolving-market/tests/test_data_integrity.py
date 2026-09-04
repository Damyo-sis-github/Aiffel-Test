"""#2 생존편향 · #8 조정가 · #9 캘린더 · #10 환율 · #13 스키마 계약."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from app.data.integrity import Grade, run_price_gate
from app.data.pit_store import PITStore
from app.data.schema import PRICES, SchemaError, coerce
from app.util.calendars import is_trading_day, next_trading_day, trading_days, us_holidays


# ---------------------------------------------------------------- #2 생존편향
def test_universe_snapshot_contains_delisted(seeded):
    """스냅샷에 상폐 종목이 최소 1개는 있어야 한다. 없으면 생존편향이다."""
    u = PITStore().universe(dt.date(2020, 11, 30))
    assert not u.empty
    assert (~u["listed"]).sum() >= 1, "상폐 종목이 스냅샷에 없습니다 — 생존편향 위험"


def test_snapshot_is_point_in_time(seeded):
    from app.universe.snapshot import UniverseBuilder

    UniverseBuilder().write(dt.date(2019, 6, 28))
    early = PITStore().universe(dt.date(2019, 6, 28))
    assert early["snapshot_date"].max() <= pd.Timestamp(dt.date(2019, 6, 28))


# ---------------------------------------------------------------- #13 스키마 계약
def test_schema_rejects_missing_column():
    df = pd.DataFrame({"symbol": ["A"], "market": ["US"]})
    with pytest.raises(SchemaError, match="필수 컬럼 누락"):
        coerce(PRICES, df)


def test_schema_rejects_unknown_column(seeded):
    store = PITStore()
    df = store.read_unfiltered("prices").head(3).drop(columns=["year"])
    df["새컬럼"] = 1
    with pytest.raises(SchemaError, match="알 수 없는 컬럼"):
        coerce(PRICES, df)


# ---------------------------------------------------------------- #4.3 게이트
def _frame(**over) -> pd.DataFrame:
    base = {
        "symbol": ["AAA"] * 3, "market": ["US"] * 3,
        "event_date": pd.to_datetime(["2020-06-01", "2020-06-02", "2020-06-03"]),
        "as_of": pd.to_datetime(["2020-06-01", "2020-06-02", "2020-06-03"]),
        "open": [10.0, 10.0, 10.0], "high": [11.0, 11.0, 11.0],
        "low": [9.0, 9.0, 9.0], "close": [10.5, 10.5, 10.5], "volume": [1e6] * 3,
        "ingested_at": pd.to_datetime(["2020-06-01", "2020-06-02", "2020-06-03"]),
    }
    base.update(over)
    return pd.DataFrame(base)


def test_ohlc_logic_quarantines():
    df = _frame(low=[12.0, 9.0, 9.0])          # low > min(o,c)
    rep = run_price_gate(df, run_date=dt.date(2020, 6, 3), market="US")
    assert "AAA" in rep.quarantined


def test_nonpositive_price_quarantines():
    df = _frame(close=[0.0, 10.5, 10.5], low=[0.0, 9.0, 9.0])
    rep = run_price_gate(df, run_date=dt.date(2020, 6, 3), market="US")
    assert "AAA" in rep.quarantined


def test_future_date_halts():
    """ingested_at < event_date → 미래를 미리 안 셈. 중단(G3)."""
    df = _frame(ingested_at=pd.to_datetime(["2020-05-01"] * 3))
    rep = run_price_gate(df, run_date=dt.date(2020, 6, 3), market="US")
    assert rep.halted


def test_us_holiday_data_halts():
    """#9 휴장일에 데이터가 있으면 US 는 중단."""
    xmas = pd.Timestamp("2020-12-25")
    df = _frame(event_date=[xmas] * 3, as_of=[xmas] * 3, ingested_at=[xmas] * 3)
    rep = run_price_gate(df, run_date=dt.date(2020, 12, 28), market="US")
    assert rep.halted
    assert any(f.check == "캘린더" and f.grade is Grade.HALT for f in rep.findings)


def test_price_jump_flags():
    df = _frame(close=[10.0, 30.0, 10.0])
    rep = run_price_gate(df, run_date=dt.date(2020, 6, 3), market="US")
    assert any(f.check == "가격 점프" for f in rep.by_grade(Grade.FLAG))


def test_leveraged_etf_gets_wider_jump_band():
    df = _frame(symbol=["TQQQ"] * 3, close=[10.0, 16.0, 10.0])   # +60% < 80% 한도
    rep = run_price_gate(df, run_date=dt.date(2020, 6, 3), market="US", kinds={"TQQQ": "lev_etf"})
    assert not any(f.check == "가격 점프" for f in rep.findings)


# ---------------------------------------------------------------- #9 캘린더
def test_us_calendar_known_holidays():
    h2021 = us_holidays(2021)
    assert dt.date(2021, 7, 5) in h2021        # 7/4 일요일 → 7/5 대체
    assert dt.date(2021, 12, 24) in h2021      # 12/25 토요일 → 12/24 대체
    assert dt.date(2021, 4, 2) in h2021        # Good Friday
    assert dt.date(2021, 11, 25) in h2021      # Thanksgiving
    assert dt.date(2022, 6, 20) in us_holidays(2022)   # Juneteenth 관측일 (6/19 일요일)
    assert dt.date(2021, 6, 18) not in us_holidays(2021)  # 2022년부터 시행


def test_next_trading_day_skips_weekend_and_holiday():
    # 2020-12-24(목) 다음 거래일은 12/25 휴장 → 12/28(월)
    assert next_trading_day("US", dt.date(2020, 12, 24)) == dt.date(2020, 12, 28)
    assert not is_trading_day("US", dt.date(2020, 12, 25))


def test_trading_days_are_sorted_and_unique():
    days = trading_days("US", dt.date(2020, 1, 1), dt.date(2020, 3, 31))
    assert days == sorted(days)
    assert len(days) == len(set(days))


# ---------------------------------------------------------------- #10 환율
def test_fx_separate_accounting(seeded):
    """USD 자산은 그날의 PIT 환율로만 환산된다. 통화 혼합은 예외."""
    from app.execution.broker import Money

    with pytest.raises(ValueError, match="통화가 다릅니다"):
        Money(100, "KRW") + Money(1, "USD")


def test_fx_forward_fill(seeded):
    from app.backtest.engine import PriceBook

    store = PITStore()
    fx = store.fx(dt.date(2020, 6, 30), pair="USDKRW")
    book = PriceBook(store.prices(dt.date(2020, 6, 30), symbols=["AAPL"]), fx)
    # 주말(환율 없음)에도 전일 값이 이월되어야 한다
    r_fri = book.fx_rate(PriceBook.to_ord(dt.date(2020, 6, 26)))
    r_sun = book.fx_rate(PriceBook.to_ord(dt.date(2020, 6, 28)))
    assert r_sun == pytest.approx(r_fri)


# ---------------------------------------------------------------- #8 조정가
def test_adjusted_prices_are_ratio_based(seeded):
    """조정계수는 분리 보관되고 원본 가격은 불변이어야 한다."""
    df = PITStore().prices(dt.date(2020, 6, 30), symbols=["AAPL"], adjusted=True)
    assert {"close", "adj_close", "adj_factor"} <= set(df.columns)
    assert (df["adj_close"] == df["close"] * df["adj_factor"].fillna(1.0)).all()
