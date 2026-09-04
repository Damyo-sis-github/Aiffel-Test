"""시장별 거래일 캘린더 (#9 캘린더·시간대).

설계:
  - US: 전부 규칙 기반으로 유도 가능(연방 휴일 + Good Friday + 주말 대체 규칙).
  - KR: 음력 휴일(설·추석·석가탄신일)은 규칙으로 유도할 수 없다.
        → `config/holidays_kr.yaml` 테이블을 쓰고, pykrx 가 있으면 그것을 **정답**으로 삼아
          테이블을 자동 보정한다(`refresh_kr_from_pykrx`).
  - 테이블이 커버하지 않는 연도는 CalendarCoverageError 를 던진다. 무결성 게이트는 이 경우
    "중단" 이 아니라 "플래그" 로 처리한다 — 캘린더 무지 때문에 시스템이 멈추면 안 된다.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from typing import Literal

import yaml

from app.paths import config_dir

Market = Literal["KR", "US"]


class CalendarCoverageError(RuntimeError):
    """해당 연도의 휴장일 정보를 신뢰할 수 없음."""


# ------------------------------------------------------------------ 공통 헬퍼


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """그 달의 n번째 weekday (월=0). n=-1 이면 마지막."""
    if n > 0:
        d = date(year, month, 1)
        offset = (weekday - d.weekday()) % 7
        return d + timedelta(days=offset + 7 * (n - 1))
    d = date(year, month, 1) + timedelta(days=32)
    d = date(d.year, d.month, 1) - timedelta(days=1)  # 말일
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, 0
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    lm = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lm) // 451
    month = (h + lm - 7 * m + 114) // 31
    day = ((h + lm - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed_us(d: date) -> date | None:
    """토요일 휴일은 전 금요일, 일요일 휴일은 다음 월요일로 대체."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


# ------------------------------------------------------------------ US


@lru_cache(maxsize=64)
def us_holidays(year: int) -> frozenset[date]:
    """NYSE/Nasdaq 정규 휴장일. Juneteenth 는 2022년부터."""
    out: set[date] = set()

    def add(d: date | None) -> None:
        if d is not None and d.year == year:
            out.add(d)

    add(_observed_us(date(year, 1, 1)))                       # New Year's Day
    add(_nth_weekday(year, 1, 0, 3))                          # MLK (3rd Mon Jan), 1998~
    add(_nth_weekday(year, 2, 0, 3))                          # Presidents' Day
    add(_easter(year) - timedelta(days=2))                    # Good Friday
    add(_nth_weekday(year, 5, 0, -1))                         # Memorial Day (last Mon May)
    if year >= 2022:
        add(_observed_us(date(year, 6, 19)))                  # Juneteenth
    add(_observed_us(date(year, 7, 4)))                       # Independence Day
    add(_nth_weekday(year, 9, 0, 1))                          # Labor Day
    add(_nth_weekday(year, 11, 3, 4))                         # Thanksgiving (4th Thu Nov)
    add(_observed_us(date(year, 12, 25)))                     # Christmas
    return frozenset(out)


# ------------------------------------------------------------------ KR


def _kr_table_path():
    return config_dir() / "holidays_kr.yaml"


@lru_cache(maxsize=1)
def _kr_table() -> dict:
    p = _kr_table_path()
    if not p.exists():
        return {"verified_through": None, "years": {}}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {"verified_through": None, "years": {}}


def _kr_fixed(year: int) -> set[date]:
    """양력 고정 공휴일 + KRX 연말 휴장. 대체공휴일 규칙은 테이블에서 보완한다."""
    out = {
        date(year, 1, 1),    # 신정
        date(year, 3, 1),    # 삼일절
        date(year, 5, 5),    # 어린이날
        date(year, 6, 6),    # 현충일
        date(year, 8, 15),   # 광복절
        date(year, 10, 3),   # 개천절
        date(year, 10, 9),   # 한글날
        date(year, 12, 25),  # 성탄절
        date(year, 12, 31),  # KRX 연말 휴장일
    }
    return {d for d in out if d.weekday() < 5}


@lru_cache(maxsize=64)
def kr_holidays(year: int) -> frozenset[date]:
    table = _kr_table()
    years = table.get("years") or {}
    entry = years.get(year) or years.get(str(year))
    if entry is None:
        raise CalendarCoverageError(
            f"KR {year}년 휴장일 테이블이 없습니다. "
            "config/holidays_kr.yaml 에 추가하거나 `quant calendar --refresh-kr` 로 pykrx 에서 가져오십시오."
        )
    lunar = {date.fromisoformat(s) for s in entry}
    return frozenset(_kr_fixed(year) | lunar)


def refresh_kr_from_pykrx(start_year: int, end_year: int) -> int:
    """pykrx 의 실제 영업일을 정답으로 삼아 KR 휴장일 테이블을 재생성한다.

    반환값: 기록된 연도 수. pykrx 가 없으면 ImportError.
    """
    from pykrx import stock  # 선택 의존성

    table = _kr_table()
    years = dict(table.get("years") or {})
    for y in range(start_year, end_year + 1):
        biz = {d.date() if hasattr(d, "date") else d for d in stock.get_previous_business_days(year=y, month=None)}
        weekdays = set()
        d = date(y, 1, 1)
        while d.year == y:
            if d.weekday() < 5:
                weekdays.add(d)
            d += timedelta(days=1)
        holidays = sorted(weekdays - biz)
        years[y] = [h.isoformat() for h in holidays if h not in _kr_fixed(y)]
    _kr_table_path().write_text(
        yaml.safe_dump(
            {"verified_through": end_year, "source": "pykrx", "years": years},
            allow_unicode=True,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _kr_table.cache_clear()
    kr_holidays.cache_clear()
    return end_year - start_year + 1


# ------------------------------------------------------------------ 공개 API


def holidays(market: Market, year: int) -> frozenset[date]:
    return us_holidays(year) if market == "US" else kr_holidays(year)


def is_trading_day(market: Market, d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in holidays(market, d.year)


def trading_days(market: Market, start: date, end: date) -> list[date]:
    """[start, end] 구간의 거래일. 항상 오름차순."""
    if end < start:
        return []
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in holidays(market, d.year):
            out.append(d)
        d += timedelta(days=1)
    return out


def previous_trading_day(market: Market, d: date) -> date:
    cur = d - timedelta(days=1)
    for _ in range(30):
        if is_trading_day(market, cur):
            return cur
        cur -= timedelta(days=1)
    raise CalendarCoverageError(f"{market} {d} 이전 30일 안에 거래일이 없습니다.")


def next_trading_day(market: Market, d: date) -> date:
    """§3 설계 원칙 5: 신호 t → 체결 t+1."""
    cur = d + timedelta(days=1)
    for _ in range(30):
        if is_trading_day(market, cur):
            return cur
        cur += timedelta(days=1)
    raise CalendarCoverageError(f"{market} {d} 이후 30일 안에 거래일이 없습니다.")


def kr_calendar_verified() -> bool:
    """휴장일 테이블이 pykrx 로 검증되었는가. False 면 캘린더 게이트는 플래그로만 동작한다."""
    return bool(_kr_table().get("verified", False))


def coverage_ok(market: Market, year: int) -> bool:
    try:
        holidays(market, year)
    except CalendarCoverageError:
        return False
    return True
