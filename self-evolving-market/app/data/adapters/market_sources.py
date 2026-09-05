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

        frames = []
        for sym in syms:
            sub = raw[sym] if isinstance(raw.columns, pd.MultiIndex) else raw
            if sub is None or sub.empty:
                continue
            sub = sub.dropna(subset=["Close"])
            # adj_factor = Adj Close / Close. 원본 가격은 불변으로 둔다 (#8).
            adj = (sub["Adj Close"] / sub["Close"]).fillna(1.0) if "Adj Close" in sub else 1.0
            frames.append(
                pd.DataFrame(
                    {
                        "symbol": sym,
                        "event_date": pd.to_datetime(sub.index).normalize(),
                        "open": sub["Open"].to_numpy(),
                        "high": sub["High"].to_numpy(),
                        "low": sub["Low"].to_numpy(),
                        "close": sub["Close"].to_numpy(),
                        "volume": sub["Volume"].to_numpy(),
                        "adj_factor": adj if isinstance(adj, float) else adj.to_numpy(),
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
            if df is None or df.empty:
                continue
            df = df.rename(
                columns={"시가": "open", "고가": "high", "저가": "low", "종가": "close", "거래량": "volume"}
            )
            frames.append(
                pd.DataFrame(
                    {
                        "symbol": sym,
                        "event_date": pd.to_datetime(df.index).normalize(),
                        "open": df["open"].to_numpy(float),
                        "high": df["high"].to_numpy(float),
                        "low": df["low"].to_numpy(float),
                        "close": df["close"].to_numpy(float),
                        "volume": df["volume"].to_numpy(float),
                        "adj_factor": 1.0,
                    }
                )
            )
        if not frames:
            raise SourceUnavailable("pykrx 에서 유효한 시계열을 받지 못했습니다.")
        return pd.concat(frames, ignore_index=True)[PRICE_COLUMNS]


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
            if df is None or df.empty:
                continue
            frames.append(
                pd.DataFrame(
                    {
                        "symbol": sym,
                        "event_date": pd.to_datetime(df.index).normalize(),
                        "open": df["Open"].to_numpy(float),
                        "high": df["High"].to_numpy(float),
                        "low": df["Low"].to_numpy(float),
                        "close": df["Close"].to_numpy(float),
                        "volume": df.get("Volume", pd.Series(0.0, index=df.index)).to_numpy(float),
                        "adj_factor": 1.0,
                    }
                )
            )
        if not frames:
            raise SourceUnavailable("fdr 에서 유효한 시계열을 받지 못했습니다.")
        return pd.concat(frames, ignore_index=True)[PRICE_COLUMNS]


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
            if s is not None and len(s) and {"date", "realtime_start", "value"} <= set(s.columns):
                sub = s.dropna(subset=["value"])
                rows.extend(
                    {
                        "series_id": sid,
                        "event_date": pd.Timestamp(r["date"]).normalize(),
                        "release_date": pd.Timestamp(r["realtime_start"]).normalize(),
                        "value": float(r["value"]),
                    }
                    for _, r in sub.iterrows()
                )
                continue
            # 폴백: 발표 지연을 모르면 보수적으로 +1일 지연을 가정한다(룩어헤드 방지 방향).
            plain = fred.get_series(sid, observation_start=start, observation_end=end)
            if plain is None or len(plain) == 0:
                continue
            rows.extend(
                {
                    "series_id": sid,
                    "event_date": pd.Timestamp(idx).normalize(),
                    "release_date": pd.Timestamp(idx).normalize() + pd.Timedelta(days=1),
                    "value": float(v),
                }
                for idx, v in plain.dropna().items()
            )
        if not rows:
            raise SourceUnavailable("FRED 에서 유효한 시계열을 받지 못했습니다.")
        df = pd.DataFrame(rows)
        return df[(df["event_date"] >= pd.Timestamp(start)) & (df["event_date"] <= pd.Timestamp(end))][
            MACRO_COLUMNS
        ]
