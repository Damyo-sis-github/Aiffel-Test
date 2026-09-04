"""§7.1 전략 인터페이스.

계약
  - `signals` 는 **순수 함수**다. I/O 금지, 전역 상태 금지, 난수는 `seed` 로만.
  - 입력 `feats` 는 PIT 쿼리 결과(그 날짜의 종목별 스냅샷 + 레짐/매크로 브로드캐스트 컬럼)뿐이다.
  - 출력은 columns: symbol, score, side(+1/-1/0).
  - 손절·보유기간·익스포저는 전략이 정하지 않는다. 엔진 하드 제약이다 (§7.6, #27).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

import pandas as pd

Family = Literal["LONG", "SHORT_US", "INV_ETF", "LEV_ETF", "ETF_ROT"]
FAMILIES: tuple[str, ...] = ("LONG", "SHORT_US", "INV_ETF", "LEV_ETF", "ETF_ROT")
GATED_FAMILIES: tuple[str, ...] = ("SHORT_US", "LEV_ETF", "INV_ETF")

SIGNAL_COLUMNS = ("symbol", "score", "side")


class SignalContractError(ValueError):
    pass


@dataclass(frozen=True)
class Signal:
    symbol: str
    score: float
    side: int


@runtime_checkable
class Strategy(Protocol):
    id: str
    version: str
    family: Family
    horizon_days: int
    universe: str
    params: dict

    def signals(self, feats: pd.DataFrame, date: dt.date) -> pd.DataFrame: ...


@dataclass
class StrategyBase:
    """시드 전략의 공통 골격. 하위 클래스는 `_rank` 만 구현하면 된다."""

    id: str = ""
    version: str = "1.0.0"
    family: Family = "LONG"
    horizon_days: int = 5
    universe: str = "ALL"
    params: dict = field(default_factory=dict)
    seed: int = 0

    # ------------------------------------------------------------ 훅

    def _rank(self, feats: pd.DataFrame, date: dt.date) -> pd.DataFrame:
        raise NotImplementedError

    def eligible_kinds(self) -> tuple[str, ...]:
        return {
            "LONG": ("stock",),
            "SHORT_US": ("stock",),
            "ETF_ROT": ("etf", "country_etf"),
            "LEV_ETF": ("lev_etf",),
            "INV_ETF": ("inv_etf",),
        }[self.family]

    # ------------------------------------------------------------ 공개 API

    def signals(self, feats: pd.DataFrame, date: dt.date) -> pd.DataFrame:
        if feats is None or feats.empty:
            return _empty_signals()
        out = self._rank(feats, date)
        return validate_signals(out, self.id)

    def describe(self) -> dict:
        return {
            "id": self.id,
            "version": self.version,
            "family": self.family,
            "horizon_days": self.horizon_days,
            "universe": self.universe,
            "params": dict(sorted(self.params.items())),
        }


def _empty_signals() -> pd.DataFrame:
    return pd.DataFrame({"symbol": pd.Series(dtype="object"),
                         "score": pd.Series(dtype="float64"),
                         "side": pd.Series(dtype="int64")})


def validate_signals(df: pd.DataFrame, strategy_id: str) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return _empty_signals()
    missing = [c for c in SIGNAL_COLUMNS if c not in df.columns]
    if missing:
        raise SignalContractError(f"[{strategy_id}] signals 출력 컬럼 누락: {missing}")
    out = df.loc[:, list(SIGNAL_COLUMNS)].copy()
    out["side"] = out["side"].astype(int)
    bad = ~out["side"].isin((-1, 0, 1))
    if bad.any():
        raise SignalContractError(f"[{strategy_id}] side 는 -1/0/+1 이어야 합니다: {out.loc[bad, 'side'].unique()}")
    out["score"] = pd.to_numeric(out["score"], errors="coerce")
    out = out[out["side"] != 0].dropna(subset=["score"])
    # 결정론: 점수 동률은 심볼 사전순으로 깬다.
    return out.sort_values(["score", "symbol"], ascending=[False, True]).reset_index(drop=True)


def restrict_kinds(feats: pd.DataFrame, kinds: tuple[str, ...]) -> pd.DataFrame:
    if "kind" not in feats.columns:
        return feats
    return feats[feats["kind"].isin(kinds)]


def pick(feats: pd.DataFrame, score: pd.Series, side: int, n: int) -> pd.DataFrame:
    """점수 상위 n개를 신호로. 동률은 symbol 사전순으로 깨서 결정론을 보장한다."""
    s = pd.to_numeric(score, errors="coerce").dropna()
    if s.empty:
        return _empty_signals()
    ordered = pd.DataFrame({"symbol": s.index.astype(str), "score": s.to_numpy()})
    ordered = ordered.sort_values(["score", "symbol"], ascending=[False, True]).head(n)
    ordered["side"] = int(side)
    return ordered.reset_index(drop=True)
