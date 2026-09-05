"""§4.3 인제스트 무결성 게이트.

등급
  QUARANTINE : 해당 심볼을 당일 신호에서 제외 + 리포트 기록
  FLAG       : 기록만 (수동 확인 대상)
  HALT       : 실행 중단 + 텔레그램 즉시 알림. 재개는 사람 게이트 G3.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
import pandas as pd

from app.util.calendars import CalendarCoverageError, kr_calendar_verified, trading_days


class Grade(StrEnum):
    QUARANTINE = "격리"
    FLAG = "플래그"
    HALT = "중단"


@dataclass
class Finding:
    check: str
    grade: Grade
    detail: str
    symbols: tuple[str, ...] = ()

    def __str__(self) -> str:
        head = f"[{self.grade.value}] {self.check}: {self.detail}"
        if self.symbols:
            shown = ", ".join(self.symbols[:8])
            more = f" 외 {len(self.symbols) - 8}건" if len(self.symbols) > 8 else ""
            head += f" ({shown}{more})"
        return head


@dataclass
class IntegrityReport:
    run_date: dt.date
    market: str
    findings: list[Finding] = field(default_factory=list)

    def add(self, *findings: Finding) -> None:
        self.findings.extend(f for f in findings if f is not None)

    @property
    def halted(self) -> bool:
        return any(f.grade is Grade.HALT for f in self.findings)

    @property
    def quarantined(self) -> tuple[str, ...]:
        out: set[str] = set()
        for f in self.findings:
            if f.grade is Grade.QUARANTINE:
                out.update(f.symbols)
        return tuple(sorted(out))

    def by_grade(self, grade: Grade) -> list[Finding]:
        return [f for f in self.findings if f.grade is grade]

    def summary(self) -> str:
        if not self.findings:
            return "무결성 게이트: 전 항목 통과"
        counts = {g.value: len(self.by_grade(g)) for g in Grade}
        return "무결성 게이트: " + ", ".join(f"{k} {v}건" for k, v in counts.items() if v)


# ------------------------------------------------------------------ 개별 검사


def check_missing_duplicate_order(df: pd.DataFrame) -> list[Finding]:
    out = []
    dup = df.duplicated(subset=["symbol", "event_date"], keep=False)
    if dup.any():
        out.append(
            Finding("중복 레코드", Grade.QUARANTINE, f"{int(dup.sum())}건", tuple(sorted(df.loc[dup, "symbol"].unique())))
        )
    nulls = df[["open", "high", "low", "close", "volume"]].isna().any(axis=1)
    if nulls.any():
        out.append(
            Finding("결측 OHLCV", Grade.QUARANTINE, f"{int(nulls.sum())}건", tuple(sorted(df.loc[nulls, "symbol"].unique())))
        )
    bad_order = []
    for sym, g in df.groupby("symbol", sort=True):
        d = g["event_date"].to_numpy()
        if len(d) > 1 and (np.diff(d) <= np.timedelta64(0, "ns")).any():
            bad_order.append(str(sym))
    if bad_order:
        out.append(Finding("역순 날짜", Grade.QUARANTINE, f"{len(bad_order)}종목", tuple(bad_order)))
    return out


def check_ohlc_logic(df: pd.DataFrame) -> list[Finding]:
    lo_bad = df["low"] > df[["open", "close"]].min(axis=1) + 1e-9
    hi_bad = df["high"] < df[["open", "close"]].max(axis=1) - 1e-9
    bad = lo_bad | hi_bad
    if not bad.any():
        return []
    return [
        Finding(
            "OHLC 논리",
            Grade.QUARANTINE,
            f"{int(bad.sum())}건 (low>min(o,c) 또는 high<max(o,c))",
            tuple(sorted(df.loc[bad, "symbol"].unique())),
        )
    ]


def check_nonpositive_price(df: pd.DataFrame) -> list[Finding]:
    bad = (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
    if not bad.any():
        return []
    return [
        Finding("0/음수 가격", Grade.QUARANTINE, f"{int(bad.sum())}건", tuple(sorted(df.loc[bad, "symbol"].unique())))
    ]


def check_zero_volume_streak(df: pd.DataFrame, min_streak: int = 5) -> list[Finding]:
    hits = []
    for sym, g in df.sort_values("event_date").groupby("symbol", sort=True):
        z = (g["volume"].fillna(0) == 0).to_numpy()
        if not z.any():
            continue
        best = cur = 0
        for v in z:
            cur = cur + 1 if v else 0
            best = max(best, cur)
        if best >= min_streak:
            hits.append(str(sym))
    if not hits:
        return []
    return [Finding("거래량 0 연속", Grade.FLAG, f"{min_streak}일 이상: {len(hits)}종목", tuple(hits))]


def check_price_jump(df: pd.DataFrame, kinds: dict[str, str] | None = None) -> list[Finding]:
    """일간 ±50% (레버리지·인버스 ETF 는 ±80%)."""
    kinds = kinds or {}
    hits: set[str] = set()
    n = 0
    for sym, g in df.sort_values("event_date").groupby("symbol", sort=True):
        limit = 0.80 if kinds.get(str(sym), "") in ("lev_etf", "inv_etf") else 0.50
        r = g["close"].pct_change()
        bad = r.abs() > limit
        if bad.any():
            hits.add(str(sym))
            n += int(bad.sum())
    if not hits:
        return []
    return [Finding("가격 점프", Grade.FLAG, f"{n}건 (수동 확인)", tuple(sorted(hits)))]


def check_calendar(df: pd.DataFrame, market: str) -> list[Finding]:
    """휴장일에 데이터가 있으면 중단(G3).

    단, KR 휴장일 테이블이 pykrx 로 검증되지 않았다면(verified: false)
    캘린더 무지 때문에 시스템이 멈추는 것을 막기 위해 플래그로 강등한다.
    """
    if df.empty:
        return []
    lo, hi = df["event_date"].min().date(), df["event_date"].max().date()
    try:
        valid = set(trading_days(market, lo, hi))
    except CalendarCoverageError as exc:
        return [Finding("캘린더", Grade.FLAG, f"휴장일 정보 없음: {exc}")]

    present = {d.date() for d in df["event_date"].unique()}
    off = sorted(present - valid)
    if not off:
        return []
    grade = Grade.HALT if (market == "US" or kr_calendar_verified()) else Grade.FLAG
    note = "" if grade is Grade.HALT else " (KR 휴장일 테이블 미검증 → 플래그로 강등)"
    return [
        Finding(
            "캘린더",
            grade,
            f"휴장일에 데이터가 있습니다: {', '.join(str(d) for d in off[:5])}{note}",
        )
    ]


def check_future_dates(df: pd.DataFrame) -> list[Finding]:
    """ingested_at < event_date → 미래를 미리 안 셈. 중단(G3)."""
    if "ingested_at" not in df.columns:
        return []
    bad = df["ingested_at"] < df["event_date"]
    if not bad.any():
        return []
    return [
        Finding(
            "미래 날짜",
            Grade.HALT,
            f"ingested_at < event_date 인 레코드 {int(bad.sum())}건",
            tuple(sorted(df.loc[bad, "symbol"].astype(str).unique())),
        )
    ]


def check_schema(df: pd.DataFrame, required: list[str]) -> list[Finding]:
    missing = [c for c in required if c not in df.columns]
    if missing:
        return [Finding("스키마 계약", Grade.HALT, f"컬럼 누락: {missing}")]
    return []


def check_fx_missing(fx: pd.DataFrame, run_date: dt.date) -> list[Finding]:
    """당일 환율 결측 → 전일 이월 + 플래그."""
    if fx.empty:
        return [Finding("환율 결측", Grade.FLAG, "환율 데이터가 없습니다. 전일 값 이월.")]
    if pd.Timestamp(run_date) not in set(fx["event_date"]):
        return [Finding("환율 결측", Grade.FLAG, f"{run_date} 환율 없음 → 전일 이월")]
    return []


def check_toss_divergence(api_close: pd.Series, toss_close: pd.Series, tol: float = 0.005) -> list[Finding]:
    """토스 CSV vs API 종가 오차 > 0.5% → 플래그 (§4.3)."""
    common = api_close.index.intersection(toss_close.index)
    if len(common) == 0:
        return []
    diff = (toss_close[common] - api_close[common]).abs() / api_close[common].replace(0, np.nan)
    bad = diff > tol
    if not bad.any():
        return []
    return [
        Finding("토스 vs API 종가", Grade.FLAG, f"{int(bad.sum())}건 오차 > {tol:.1%}", tuple(map(str, common[bad])))
    ]


# ------------------------------------------------------------------ 파이프라인

REQUIRED_PRICE_COLS = ["symbol", "market", "event_date", "as_of", "open", "high", "low", "close", "volume"]


def run_price_gate(
    df: pd.DataFrame,
    *,
    run_date: dt.date,
    market: str,
    kinds: dict[str, str] | None = None,
) -> IntegrityReport:
    rep = IntegrityReport(run_date=run_date, market=market)
    rep.add(*check_schema(df, REQUIRED_PRICE_COLS))
    if rep.halted or df.empty:
        return rep
    rep.add(*check_missing_duplicate_order(df))
    rep.add(*check_ohlc_logic(df))
    rep.add(*check_nonpositive_price(df))
    rep.add(*check_zero_volume_streak(df))
    rep.add(*check_price_jump(df, kinds))
    rep.add(*check_calendar(df, market))
    rep.add(*check_future_dates(df))
    return rep
