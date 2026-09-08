"""실제 시장 데이터 어댑터 (yfinance / pykrx / FinanceDataReader / FRED).

라이브러리가 없거나 네트워크가 막히면 SourceUnavailable 을 던진다.
레지스트리가 이를 잡아 synthetic 으로 폴백할지, 실패시킬지 결정한다.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Sequence

import pandas as pd

from app.data.adapters.base import MACRO_COLUMNS, PRICE_COLUMNS, SourceUnavailable


def _dates(index) -> pd.DatetimeIndex:
    """인덱스 → tz 없는 자정 기준 날짜.

    yfinance 는 tz 가 붙은 인덱스를 준다 (US/Eastern). 그대로 두면 parquet 에
    tz-aware 로 들어가고, PIT 조회가 tz-naive 커트오프와 비교하면서 터진다.
    합성 데이터에는 tz 가 없어서 이 문제는 실데이터로 넘어가야만 드러난다.
    """
    idx = pd.DatetimeIndex(pd.to_datetime(index))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return idx.normalize()


def _empty_prices() -> pd.DataFrame:
    return pd.DataFrame(columns=PRICE_COLUMNS)


class YFinancePriceSource:
    """US 일봉. auto_adjust=False 로 **원본 가격**을 받고 조정계수를 분리한다 (§4.1)."""

    name = "yfinance"
    market = "US"

    def fetch(self, symbols: Sequence[str], start: dt.date, end: dt.date) -> pd.DataFrame:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise SourceUnavailable("yfinance 미설치: `uv pip install -e .[market]`") from exc

        syms = sorted(set(symbols))
        if not syms:
            return _empty_prices()
        raw = yf.download(
            syms,
            start=start.isoformat(),
            end=(end + dt.timedelta(days=1)).isoformat(),
            auto_adjust=False,
            actions=False,
            progress=False,
            group_by="ticker",
            threads=True,
        )
        if raw is None or len(raw) == 0:
            raise SourceUnavailable("yfinance 응답이 비었습니다 (네트워크 또는 심볼 확인).")
        return self.normalize(raw, syms)

    @staticmethod
    def normalize(raw: pd.DataFrame, symbols: Sequence[str]) -> pd.DataFrame:
        """응답 → PIT 스키마. **순수 함수** — 네트워크 없이 테스트한다.

        여기가 실데이터로 넘어갈 때 실제로 깨지는 지점이다 (MultiIndex 여부,
        Adj Close 유무, tz 붙은 인덱스). 네트워크를 타는 테스트로는 못 잡는다.
        """
        frames = []
        for sym in sorted(set(symbols)):
            if isinstance(raw.columns, pd.MultiIndex):
                if sym not in raw.columns.get_level_values(0):
                    continue
                sub = raw[sym]
            else:
                sub = raw
            if sub is None or sub.empty or "Close" not in sub:
                continue
            sub = sub.dropna(subset=["Close"])
            if sub.empty:
                continue
            # adj_factor = Adj Close / Close. 원본 가격은 불변으로 둔다 (#8).
            adj = (sub["Adj Close"] / sub["Close"]).fillna(1.0) if "Adj Close" in sub else 1.0
            frames.append(
                pd.DataFrame(
                    {
                        "symbol": sym,
                        "event_date": _dates(sub.index),
                        "open": sub["Open"].to_numpy(float),
                        "high": sub["High"].to_numpy(float),
                        "low": sub["Low"].to_numpy(float),
                        "close": sub["Close"].to_numpy(float),
                        "volume": sub["Volume"].to_numpy(float),
                        "adj_factor": adj if isinstance(adj, float) else adj.to_numpy(float),
                    }
                )
            )
        if not frames:
            raise SourceUnavailable("yfinance 에서 유효한 시계열을 받지 못했습니다.")
        return pd.concat(frames, ignore_index=True)[PRICE_COLUMNS]


class PykrxPriceSource:
    """KR 일봉. pykrx 는 수정주가를 주므로 adj_factor=1.0 으로 둔다."""

    name = "pykrx"
    market = "KR"

    def fetch(self, symbols: Sequence[str], start: dt.date, end: dt.date) -> pd.DataFrame:
        try:
            from pykrx import stock
        except ImportError as exc:
            raise SourceUnavailable("pykrx 미설치: `uv pip install -e .[market]`") from exc

        s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        frames = []
        for sym in sorted(set(symbols)):
            try:
                df = stock.get_market_ohlcv_by_date(s, e, sym)
            except Exception as exc:  # pykrx 는 다양한 예외를 던진다
                raise SourceUnavailable(f"pykrx 조회 실패 ({sym}): {exc}") from exc
            one = self.normalize(sym, df)
            if one is not None:
                frames.append(one)
        if not frames:
            raise SourceUnavailable("pykrx 에서 유효한 시계열을 받지 못했습니다.")
        return pd.concat(frames, ignore_index=True)[PRICE_COLUMNS]

    @staticmethod
    def normalize(sym: str, df: pd.DataFrame | None) -> pd.DataFrame | None:
        """한글 컬럼 → PIT 스키마. 순수 함수."""
        if df is None or df.empty:
            return None
        df = df.rename(
            columns={"시가": "open", "고가": "high", "저가": "low", "종가": "close", "거래량": "volume"}
        )
        need = ("open", "high", "low", "close", "volume")
        if any(c not in df.columns for c in need):
            raise SourceUnavailable(
                f"pykrx 응답 컬럼이 바뀌었습니다 ({sym}): {sorted(df.columns)}"
            )
        return pd.DataFrame(
            {
                "symbol": sym,
                "event_date": _dates(df.index),
                "open": df["open"].to_numpy(float),
                "high": df["high"].to_numpy(float),
                "low": df["low"].to_numpy(float),
                "close": df["close"].to_numpy(float),
                "volume": df["volume"].to_numpy(float),
                # pykrx 는 수정주가를 준다 → 조정계수는 1.0 (§4.1)
                "adj_factor": 1.0,
            }
        )


class FdrPriceSource:
    """FinanceDataReader 폴백 (KR/US 겸용)."""

    name = "fdr"

    def __init__(self, market: str = "KR"):
        self.market = market

    def fetch(self, symbols: Sequence[str], start: dt.date, end: dt.date) -> pd.DataFrame:
        try:
            import FinanceDataReader as fdr
        except ImportError as exc:
            raise SourceUnavailable("finance-datareader 미설치") from exc

        frames = []
        for sym in sorted(set(symbols)):
            try:
                df = fdr.DataReader(sym, start.isoformat(), end.isoformat())
            except Exception as exc:
                raise SourceUnavailable(f"fdr 조회 실패 ({sym}): {exc}") from exc
            one = self.normalize(sym, df)
            if one is not None:
                frames.append(one)
        if not frames:
            raise SourceUnavailable("fdr 에서 유효한 시계열을 받지 못했습니다.")
        return pd.concat(frames, ignore_index=True)[PRICE_COLUMNS]

    @staticmethod
    def normalize(sym: str, df: pd.DataFrame | None) -> pd.DataFrame | None:
        if df is None or df.empty:
            return None
        return pd.DataFrame(
            {
                "symbol": sym,
                "event_date": _dates(df.index),
                "open": df["Open"].to_numpy(float),
                "high": df["High"].to_numpy(float),
                "low": df["Low"].to_numpy(float),
                "close": df["Close"].to_numpy(float),
                "volume": df.get("Volume", pd.Series(0.0, index=df.index)).to_numpy(float),
                "adj_factor": 1.0,
            }
        )


class FredMacroSource:
    """FRED. release_date 를 반드시 채운다 — 이것이 as_of 가 된다 (§4.1, §6.1)."""

    name = "fred"

    def fetch(self, series_ids: Sequence[str], start: dt.date, end: dt.date) -> pd.DataFrame:
        try:
            from fredapi import Fred
        except ImportError as exc:
            raise SourceUnavailable("fredapi 미설치") from exc
        api_key = os.environ.get("FRED_API_KEY")
        if not api_key:
            raise SourceUnavailable("FRED_API_KEY 가 .env 에 없습니다.")

        fred = Fred(api_key=api_key)
        rows = []
        for sid in sorted(set(series_ids)):
            try:
                # ALFRED 방식: realtime 정보를 함께 받아 release_date 를 확정한다.
                s = fred.get_series_all_releases(sid)
            except Exception:
                s = None
            if (releases := self.normalize_releases(sid, s)) is not None:
                rows.extend(releases)
                continue
            # 폴백: 발표 지연을 모르면 보수적으로 +1일 지연을 가정한다(룩어헤드 방지 방향).
            plain = fred.get_series(sid, observation_start=start, observation_end=end)
            rows.extend(self.normalize_plain(sid, plain))
        if not rows:
            raise SourceUnavailable("FRED 에서 유효한 시계열을 받지 못했습니다.")

        df = pd.DataFrame(rows)
        return df[(df["event_date"] >= pd.Timestamp(start)) & (df["event_date"] <= pd.Timestamp(end))][
            MACRO_COLUMNS
        ]

    @staticmethod
    def normalize_releases(sid: str, s: pd.DataFrame | None) -> list[dict] | None:
        """ALFRED 응답 → release_date 가 채워진 행. 못 쓰면 None (폴백 신호).

        release_date 가 as_of 가 된다. 이걸 event_date 로 잘못 채우면 발표 전
        지표를 보게 되고, 그 룩어헤드는 백테스트를 조용히 좋아 보이게 만든다.
        """
        if s is None or len(s) == 0 or not {"date", "realtime_start", "value"} <= set(s.columns):
            return None
        sub = s.dropna(subset=["value"])
        return [
            {
                "series_id": sid,
                "event_date": pd.Timestamp(r["date"]).normalize(),
                "release_date": pd.Timestamp(r["realtime_start"]).normalize(),
                "value": float(r["value"]),
            }
            for _, r in sub.iterrows()
        ]

    @staticmethod
    def normalize_plain(sid: str, plain: pd.Series | None) -> list[dict]:
        """발표일을 모르는 응답. **+1일 지연을 가정한다** — 안전한 방향으로만 틀린다."""
        if plain is None or len(plain) == 0:
            return []
        return [
            {
                "series_id": sid,
                "event_date": pd.Timestamp(idx).normalize(),
                "release_date": pd.Timestamp(idx).normalize() + pd.Timedelta(days=1),
                "value": float(v),
            }
            for idx, v in plain.dropna().items()
        ]
