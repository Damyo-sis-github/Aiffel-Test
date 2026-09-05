"""Point-in-Time 저장소. 백테스트가 미래를 볼 수 있는 유일한 통로를 여기서 막는다.

규칙 (§4.2):
  - 모든 읽기 함수는 `as_of` 를 **필수 인자**로 받는다. 기본값이 없다.
  - 반환 데이터는 `event_date <= as_of AND as_of_col <= as_of` 로 필터된다.
  - PIT 필터를 우회하는 읽기는 `read_unfiltered()` 하나뿐이며, 무결성 검사·리페어 전용이다.
    이 함수는 백테스트/피처/전략 코드에서 호출하면 안 되고, 테스트가 그것을 강제한다.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from pathlib import Path

import pandas as pd

from app.data.schema import TABLES, Table, add_year, coerce
from app.paths import data_dir

DateLike = dt.date | dt.datetime | str | pd.Timestamp


class PITViolation(RuntimeError):
    """PIT 계약 위반."""


def _ts(d: DateLike) -> pd.Timestamp:
    t = pd.Timestamp(d)
    return t.normalize()


def _to_ns(df: pd.DataFrame) -> pd.DataFrame:
    """Parquet 왕복은 datetime 해상도를 ms/us 로 바꾼다. 비교·머지 전에 ns 로 통일한다."""
    for col, dtype in df.dtypes.items():
        if dtype.kind == "M" and dtype != "datetime64[ns]":
            df[col] = df[col].astype("datetime64[ns]")
    return df


def _require_as_of(as_of: DateLike | None) -> pd.Timestamp:
    if as_of is None:
        raise PITViolation(
            "as_of 없이 PIT 저장소를 조회할 수 없습니다. "
            "백테스트 시뮬 날짜를 명시하십시오 (§4.2)."
        )
    return _ts(as_of)


class PITStore:
    """Parquet(시계열) 기반 PIT 저장소."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else data_dir() / "parquet"
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 경로

    def _table_dir(self, table: Table) -> Path:
        p = self.root / table.name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _part_path(self, table: Table, keys: dict) -> Path:
        parts = [f"{k}={keys[k]}" for k in table.partition]
        p = self._table_dir(table).joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p / "part.parquet"

    def files(self, table_name: str) -> list[Path]:
        t = TABLES[table_name]
        return sorted(self._table_dir(t).rglob("*.parquet"))

    # ------------------------------------------------------------ 쓰기

    def write(self, table_name: str, df: pd.DataFrame) -> int:
        """계약 검사 → 파티션별 업서트(PK 중복은 나중 값이 이긴다). 반환: 기록된 행 수."""
        table = TABLES[table_name]
        if df is None or len(df) == 0:
            return 0
        clean = add_year(coerce(table, df), table)
        written = 0
        for keys, chunk in clean.groupby(list(table.partition), dropna=False, sort=True):
            keymap = dict(zip(table.partition, keys if isinstance(keys, tuple) else (keys,), strict=True))
            path = self._part_path(table, keymap)
            if path.exists():
                old = pd.read_parquet(path)
                chunk = pd.concat([old, chunk.drop(columns=list(table.partition), errors="ignore")
                                   .assign(**keymap)], ignore_index=True)
            chunk = (
                chunk.drop_duplicates(subset=list(table.pk), keep="last")
                .sort_values(list(table.pk), kind="mergesort")
                .reset_index(drop=True)
            )
            chunk.to_parquet(path, index=False, compression="zstd")
            written += len(chunk)
        return written

    # ------------------------------------------------------------ 읽기 (PIT 강제)

    def _read(
        self,
        table_name: str,
        as_of: DateLike,
        *,
        where: dict[str, Sequence] | None = None,
        start: DateLike | None = None,
    ) -> pd.DataFrame:
        cutoff = _require_as_of(as_of)
        table = TABLES[table_name]
        files = self.files(table_name)
        if not files:
            return pd.DataFrame(columns=[*table.columns, "year"])

        frames = [pd.read_parquet(f) for f in files]
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=table.columns)
        if df.empty:
            return df
        df = _to_ns(df)

        date_col = "snapshot_date" if table_name == "universe_snapshot" else "event_date"
        # ★ PIT 이중 필터. 이 두 줄이 이 저장소의 존재 이유다.
        df = df[df[date_col] <= cutoff]
        df = df[df["as_of"] <= cutoff]

        if start is not None:
            df = df[df[date_col] >= _ts(start)]
        for col, values in (where or {}).items():
            if values is None:
                continue
            vals = list(values) if isinstance(values, (list, tuple, set, frozenset)) else [values]
            df = df[df[col].isin(vals)]

        return df.sort_values(list(table.pk), kind="mergesort").reset_index(drop=True)

    def prices(
        self,
        as_of: DateLike,
        *,
        symbols: Iterable[str] | None = None,
        market: str | None = None,
        start: DateLike | None = None,
        lookback_days: int | None = None,
        adjusted: bool = True,
    ) -> pd.DataFrame:
        """PIT 가격. adjusted=True 면 adj_factor 를 곱한 조정가 컬럼을 추가한다.

        원본 가격은 불변으로 남기고 조정계수를 따로 둔다 (§4.2, #8).
        """
        cutoff = _require_as_of(as_of)
        if start is None and lookback_days:
            start = cutoff - pd.Timedelta(days=int(lookback_days * 1.6) + 10)
        df = self._read(
            "prices",
            cutoff,
            where={"symbol": list(symbols) if symbols is not None else None, "market": market},
            start=start,
        )
        if df.empty or not adjusted:
            return df
        af = df["adj_factor"].fillna(1.0)
        for col in ("open", "high", "low", "close"):
            df[f"adj_{col}"] = df[col] * af
        return df

    def macro(
        self,
        as_of: DateLike,
        *,
        series_ids: Iterable[str] | None = None,
        start: DateLike | None = None,
    ) -> pd.DataFrame:
        """PIT 매크로. release_date <= as_of 인 것만 (§6.1)."""
        return self._read(
            "macro", as_of, where={"series_id": list(series_ids) if series_ids else None}, start=start
        )

    def fx(self, as_of: DateLike, *, pair: str | None = None, start: DateLike | None = None) -> pd.DataFrame:
        return self._read("fx", as_of, where={"pair": pair}, start=start)

    def universe(
        self,
        as_of: DateLike,
        *,
        market: str | None = None,
        kinds: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        """§5.6 그 시점 스냅샷. 가장 최근 snapshot_date 하나만 돌려준다 (#2 생존편향)."""
        df = self._read(
            "universe_snapshot",
            as_of,
            where={"market": market, "kind": list(kinds) if kinds else None},
        )
        if df.empty:
            return df
        latest = df["snapshot_date"].max()
        return df[df["snapshot_date"] == latest].reset_index(drop=True)

    def latest_price_date(self, as_of: DateLike, market: str) -> pd.Timestamp | None:
        df = self._read("prices", as_of, where={"market": market})
        return None if df.empty else df["event_date"].max()

    # ------------------------------------------------------------ 무결성 전용

    def read_unfiltered(self, table_name: str) -> pd.DataFrame:
        """⚠️ PIT 필터 없는 원본 읽기. **무결성 검사·리페어 전용.**

        백테스트·피처·전략·포트폴리오 코드에서 호출 금지.
        tests/test_pit_enforcement.py 가 호출처를 강제한다.
        """
        files = self.files(table_name)
        if not files:
            return pd.DataFrame(columns=TABLES[table_name].columns)
        return _to_ns(pd.concat([pd.read_parquet(f) for f in files], ignore_index=True))
