"""실데이터 어댑터의 **응답 해석**을 네트워크 없이 검증한다 (§4.1, CLAUDE.md 12).

왜 필요했나
  `offline: false` 로 넘기기 전까지 이 코드는 **한 줄도 실행되지 않았다.**
  테스트는 전부 synthetic 으로 돌고, synthetic 은 이미 PIT 스키마 모양으로 나온다.
  그래서 실데이터로 넘어가는 날 처음 도는 코드가 되고, 그날 노트북에서 깨진다.
  이 프로젝트에서 반복해서 겪은 실패 방식이다.

네트워크는 여전히 타지 않는다. 어댑터를 `fetch`(네트워크)와 `normalize`(순수)로
나눠 후자만 검증한다. 실제로 깨지는 곳은 대부분 후자다 — 컬럼명, MultiIndex 여부,
tz, 결측 컬럼, 그리고 as_of 를 무엇으로 채우는가.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from app.data.adapters.base import MACRO_COLUMNS, PRICE_COLUMNS, SourceUnavailable
from app.data.adapters.market_sources import (
    FdrPriceSource,
    FredMacroSource,
    PykrxPriceSource,
    YFinancePriceSource,
)

DAYS = pd.date_range("2024-01-02", periods=3, freq="B")


def _ohlc(index=DAYS, cols=("Open", "High", "Low", "Close", "Volume"), adj=True) -> pd.DataFrame:
    data = {c: np.arange(1.0, len(index) + 1.0) * 10 for c in cols}
    if adj:
        data["Adj Close"] = data["Close"] * 0.9
    return pd.DataFrame(data, index=index)


def _assert_contract(df: pd.DataFrame) -> None:
    assert list(df.columns) == PRICE_COLUMNS
    assert pd.api.types.is_datetime64_any_dtype(df["event_date"])
    assert getattr(df["event_date"].dt, "tz", None) is None, "tz 가 붙은 채로 나가면 PIT 조회가 깨집니다"
    assert (df["event_date"] == df["event_date"].dt.normalize()).all()
    for c in ("open", "high", "low", "close", "volume", "adj_factor"):
        assert pd.api.types.is_float_dtype(df[c]), c


# ------------------------------------------------------------ yfinance
def test_yf_multi_symbol_response():
    raw = pd.concat({"AAPL": _ohlc(), "MSFT": _ohlc()}, axis=1)
    out = YFinancePriceSource.normalize(raw, ["MSFT", "AAPL"])
    _assert_contract(out)
    assert sorted(out["symbol"].unique()) == ["AAPL", "MSFT"]
    assert len(out) == 6


def test_yf_single_symbol_response_is_flat():
    """심볼 하나면 yfinance 가 MultiIndex 를 주지 않는다. 둘 다 처리해야 한다."""
    out = YFinancePriceSource.normalize(_ohlc(), ["AAPL"])
    _assert_contract(out)
    assert len(out) == 3


def test_yf_adj_factor_is_ratio_not_price():
    """#8 원본 가격은 불변, 조정은 계수로 분리한다."""
    out = YFinancePriceSource.normalize(_ohlc(), ["AAPL"])
    assert out["adj_factor"].round(6).eq(0.9).all()
    assert out["close"].iloc[0] == 10.0, "원본 종가가 조정되어 버렸습니다"


def test_yf_without_adj_close_falls_back_to_one():
    out = YFinancePriceSource.normalize(_ohlc(adj=False), ["AAPL"])
    assert out["adj_factor"].eq(1.0).all()


def test_yf_strips_timezone():
    """실데이터 인덱스에는 tz 가 붙어 온다. 합성 데이터에는 없어서 여기서만 드러난다."""
    # 고정 오프셋을 쓴다 — tzdata 가 없는 환경에서도 같은 경로를 탄다.
    tz = DAYS.tz_localize(dt.timezone(dt.timedelta(hours=-5)))
    out = YFinancePriceSource.normalize(_ohlc(index=tz), ["AAPL"])
    _assert_contract(out)
    assert out["event_date"].iloc[0] == pd.Timestamp("2024-01-02")


def test_yf_skips_symbols_missing_from_the_response():
    raw = pd.concat({"AAPL": _ohlc()}, axis=1)
    out = YFinancePriceSource.normalize(raw, ["AAPL", "NOPE"])
    assert sorted(out["symbol"].unique()) == ["AAPL"]


def test_yf_all_empty_is_an_error_not_an_empty_frame():
    """빈 프레임을 조용히 돌려주면 무결성 게이트가 '데이터 없음'을 정상으로 본다."""
    with pytest.raises(SourceUnavailable):
        YFinancePriceSource.normalize(pd.DataFrame(), ["AAPL"])


# ------------------------------------------------------------ pykrx
def test_pykrx_korean_columns():
    df = pd.DataFrame(
        {"시가": [1.0, 2, 3], "고가": [1.0, 2, 3], "저가": [1.0, 2, 3],
         "종가": [1.0, 2, 3], "거래량": [10.0, 20, 30]},
        index=DAYS,
    )
    out = PykrxPriceSource.normalize("005930", df)
    _assert_contract(out)
    assert out["symbol"].eq("005930").all()
    assert out["adj_factor"].eq(1.0).all(), "pykrx 는 수정주가라 계수는 1.0 이어야 합니다"


def test_pykrx_column_rename_is_detected():
    """pykrx 가 컬럼명을 바꾸면 조용히 빈 값이 아니라 오류로 드러나야 한다."""
    df = pd.DataFrame({"open": [1.0], "고가": [1.0]}, index=DAYS[:1])
    with pytest.raises(SourceUnavailable, match="컬럼이 바뀌었습니다"):
        PykrxPriceSource.normalize("005930", df)


def test_pykrx_empty_is_skipped_not_raised():
    assert PykrxPriceSource.normalize("005930", pd.DataFrame()) is None


# ------------------------------------------------------------ fdr
def test_fdr_without_volume_column():
    df = _ohlc(cols=("Open", "High", "Low", "Close"), adj=False)
    out = FdrPriceSource.normalize("AAPL", df)
    _assert_contract(out)
    assert out["volume"].eq(0.0).all()


# ------------------------------------------------------------ FRED / as_of
def test_fred_uses_realtime_start_as_release_date():
    """as_of = release_date. event_date 로 채우면 발표 전 지표를 보게 된다 (룩어헤드)."""
    s = pd.DataFrame({
        "date": ["2024-01-31", "2024-02-29"],
        "realtime_start": ["2024-02-15", "2024-03-15"],
        "value": [1.0, 2.0],
    })
    rows = FredMacroSource.normalize_releases("CPIAUCSL", s)
    assert rows is not None and len(rows) == 2
    assert rows[0]["event_date"] == pd.Timestamp("2024-01-31")
    assert rows[0]["release_date"] == pd.Timestamp("2024-02-15")
    assert rows[0]["release_date"] > rows[0]["event_date"], "발표일이 관측일보다 앞설 수 없습니다"
    assert set(rows[0]) == set(MACRO_COLUMNS)


def test_fred_falls_back_to_plus_one_day():
    """발표일을 모르면 하루 늦게 안다고 가정한다 — 틀리더라도 안전한 방향으로."""
    plain = pd.Series([1.0, 2.0], index=pd.to_datetime(["2024-01-31", "2024-02-29"]))
    rows = FredMacroSource.normalize_plain("DGS10", plain)
    assert len(rows) == 2
    for r in rows:
        assert r["release_date"] == r["event_date"] + pd.Timedelta(days=1)


def test_fred_returns_none_when_release_info_is_missing():
    """폴백으로 넘어가야 할 응답을 잘못 받아들이면 as_of 가 비어버린다."""
    assert FredMacroSource.normalize_releases("X", pd.DataFrame({"date": [], "value": []})) is None
    assert FredMacroSource.normalize_releases("X", None) is None


def test_fred_drops_missing_values():
    s = pd.DataFrame({
        "date": ["2024-01-31", "2024-02-29"],
        "realtime_start": ["2024-02-15", "2024-03-15"],
        "value": [1.0, None],
    })
    assert len(FredMacroSource.normalize_releases("X", s)) == 1


# ------------------------------------------------------------ 인제스트와의 계약
def test_normalized_frames_pass_the_ingest_contract(sandbox):
    """어댑터 출력이 그대로 PIT 저장소로 들어갈 수 있는지."""
    from app.data.adapters.base import validate_price_frame

    out = YFinancePriceSource.normalize(_ohlc(), ["AAPL"])
    checked = validate_price_frame(out, "yfinance")
    assert list(checked.columns) == PRICE_COLUMNS
    # as_of 는 인제스트가 채운다 (prices.as_of = event_date). 어댑터가 넣으면 안 된다.
    assert "as_of" not in out.columns


def test_adapters_are_not_reachable_when_offline(sandbox):
    """`offline: true` 인 동안에는 어떤 어댑터도 네트워크를 타지 않는다."""
    from app.data.adapters.registry import active_source_labels, synthetic_in_use

    assert synthetic_in_use()
    assert set(active_source_labels().values()) == {"synthetic"}


def test_ingest_stamps_as_of_from_event_date(seeded):
    """CLAUDE.md 12 — 새 소스는 as_of 를 어떻게 채우는지부터 정한다."""
    from app.data.pit_store import PITStore

    df = PITStore().read_unfiltered("prices")
    assert (df["as_of"] == df["event_date"]).all(), "가격의 as_of 는 event_date 여야 합니다"
