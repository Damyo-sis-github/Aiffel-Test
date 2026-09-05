from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import pandas as pd


class SourceUnavailable(RuntimeError):
    """라이브러리 미설치 또는 네트워크 불가."""


@runtime_checkable
class PriceSource(Protocol):
    name: str
    market: str

    def fetch(self, symbols: Sequence[str], start: dt.date, end: dt.date) -> pd.DataFrame:
        """columns: symbol, event_date, open, high, low, close, volume, adj_factor

        as_of / source / ingested_at 는 인제스트 계층이 채운다.
        """
        ...


@runtime_checkable
class MacroSource(Protocol):
    name: str

    def fetch(self, series_ids: Sequence[str], start: dt.date, end: dt.date) -> pd.DataFrame:
        """columns: series_id, event_date, release_date, value"""
        ...


PRICE_COLUMNS = ["symbol", "event_date", "open", "high", "low", "close", "volume", "adj_factor"]
MACRO_COLUMNS = ["series_id", "event_date", "release_date", "value"]


def validate_price_frame(df: pd.DataFrame, source: str) -> pd.DataFrame:
    missing = [c for c in PRICE_COLUMNS if c not in df.columns]
    if missing:
        raise SourceUnavailable(f"[{source}] 가격 프레임 컬럼 누락: {missing}")
    return df.loc[:, PRICE_COLUMNS]
