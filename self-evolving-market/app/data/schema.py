"""§4.2 저장 스키마 — Point-in-Time.

as_of / event_date 의미 (이 정의를 어기면 룩어헤드다):
  event_date : 레코드가 **가리키는** 날짜
  as_of      : 그 정보를 **알 수 있게 된** 날짜
    - prices : as_of == event_date (그날 종가는 그날 장 마감에 알려진다)
    - macro  : as_of == release_date (발표일. event_date 는 지표가 가리키는 기간)
    - universe_snapshot : as_of == snapshot_date
  ingested_at: 우리가 디스크에 쓴 날짜. **PIT 필터에 쓰지 않는다.**
               무결성 검사(#미래 날짜)에서 ingested_at < event_date 를 잡는 데만 쓴다.

백테스트 쿼리는 `event_date <= sim_date AND as_of <= sim_date` 를 강제한다.
이 조건 없는 가격 조회 함수는 이 저장소에 존재해서는 안 된다 (tests/test_pit_enforcement.py).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Table:
    name: str
    pk: tuple[str, ...]
    partition: tuple[str, ...]
    dtypes: dict[str, str]

    @property
    def columns(self) -> list[str]:
        return list(self.dtypes)


PRICES = Table(
    name="prices",
    pk=("symbol", "market", "event_date"),
    partition=("market", "year"),
    dtypes={
        "symbol": "string",
        "market": "string",
        "event_date": "datetime64[ns]",
        "as_of": "datetime64[ns]",
        "open": "float64",
        "high": "float64",
        "low": "float64",
        "close": "float64",
        "volume": "float64",
        "adj_factor": "float64",
        "source": "string",
        "ingested_at": "datetime64[ns]",
    },
)

UNIVERSE_SNAPSHOT = Table(
    name="universe_snapshot",
    pk=("snapshot_date", "symbol", "market"),
    partition=("market", "year"),
    dtypes={
        "snapshot_date": "datetime64[ns]",
        "as_of": "datetime64[ns]",
        "event_date": "datetime64[ns]",
        "symbol": "string",
        "market": "string",
        "kind": "string",          # stock|etf|lev_etf|inv_etf|country_etf
        "listed": "bool",
        "delist_date": "datetime64[ns]",
        "mcap": "float64",
        "adv20": "float64",
        "leverage": "float64",
        "sector": "string",
        "theme": "string",
        "source": "string",
        "ingested_at": "datetime64[ns]",
    },
)

MACRO = Table(
    name="macro",
    pk=("series_id", "event_date"),
    partition=("year",),
    dtypes={
        "series_id": "string",
        "event_date": "datetime64[ns]",
        "release_date": "datetime64[ns]",
        "as_of": "datetime64[ns]",     # == release_date
        "value": "float64",
        "source": "string",
        "ingested_at": "datetime64[ns]",
    },
)

FX = Table(
    name="fx",
    pk=("pair", "event_date"),
    partition=("year",),
    dtypes={
        "pair": "string",
        "event_date": "datetime64[ns]",
        "as_of": "datetime64[ns]",
        "rate": "float64",
        "source": "string",
        "ingested_at": "datetime64[ns]",
    },
)

TABLES: dict[str, Table] = {t.name: t for t in (PRICES, UNIVERSE_SNAPSHOT, MACRO, FX)}

# PIT 필터가 반드시 적용되어야 하는 테이블
PIT_TABLES = frozenset(TABLES)


class SchemaError(ValueError):
    """#13 소스 스키마 변경 — 어댑터 계약 위반."""


def coerce(table: Table, df: pd.DataFrame) -> pd.DataFrame:
    """계약 검사 + 타입 캐스팅. 컬럼 누락/여분은 즉시 예외 (#13)."""
    missing = [c for c in table.columns if c not in df.columns]
    if missing:
        raise SchemaError(f"[{table.name}] 필수 컬럼 누락: {missing}")
    extra = [c for c in df.columns if c not in table.dtypes]
    if extra:
        raise SchemaError(f"[{table.name}] 알 수 없는 컬럼: {extra}. 어댑터에서 정규화하십시오.")

    out = df.loc[:, table.columns].copy()
    for col, dtype in table.dtypes.items():
        if dtype.startswith("datetime"):
            out[col] = pd.to_datetime(out[col], errors="coerce").dt.normalize()
        elif dtype == "bool":
            out[col] = out[col].astype("boolean").fillna(False).astype(bool)
        elif dtype == "string":
            out[col] = out[col].astype("string")
        else:
            out[col] = pd.to_numeric(out[col], errors="coerce").astype(dtype)
    return out


def add_year(df: pd.DataFrame, table: Table) -> pd.DataFrame:
    """파티션 키 year 파생."""
    key = "snapshot_date" if table is UNIVERSE_SNAPSHOT else "event_date"
    out = df.copy()
    out["year"] = pd.to_datetime(out[key]).dt.year.astype("int32")
    return out
