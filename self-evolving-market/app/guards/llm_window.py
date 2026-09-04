"""#29 낮 시간 LLM 실행 거부·이월 (§11.3).

평일 08:00–18:00 KST 는 본인이 다른 개발에 Claude 를 쓴다. 이 창에서 /evolve 는
실행을 거부하고 다음 창(당일 19:00 또는 다음 거래 가능 시각)으로 이월한다.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.config import llm_window
from app.guards.base import GuardResult
from app.util.calendars import CalendarCoverageError, is_trading_day

_WD = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


def _tz() -> ZoneInfo:
    return ZoneInfo(llm_window().get("timezone", "Asia/Seoul"))


def now_kst() -> datetime:
    return datetime.now(_tz())


def _parse_hhmm(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


def _is_holiday(d: date) -> bool:
    try:
        return not is_trading_day("KR", d)
    except CalendarCoverageError:
        return d.weekday() >= 5


def in_blocked_window(when: datetime | None = None) -> tuple[bool, str]:
    cfg = llm_window()
    when = when or now_kst()
    if when.tzinfo is None:
        when = when.replace(tzinfo=_tz())
    bw = cfg.get("blocked_window") or {}
    days = {_WD[str(d).upper()] for d in (bw.get("weekdays") or []) if str(d).upper() in _WD}

    if when.weekday() not in days:
        return False, "차단 요일 아님"
    if cfg.get("weekend_unrestricted", True) and when.weekday() >= 5:
        return False, "주말"
    if cfg.get("holiday_unrestricted", True) and _is_holiday(when.date()):
        return False, "공휴일"

    start, end = _parse_hhmm(bw.get("start", "08:00")), _parse_hhmm(bw.get("end", "18:00"))
    t = when.timetz().replace(tzinfo=None)
    if start <= t < end:
        return True, f"본인 사용 시간대 {bw.get('start')}–{bw.get('end')} KST"
    return False, "창 밖"


def next_allowed_llm_time(when: datetime | None = None) -> datetime:
    """이월 목적지. 차단 창 안이면 당일 예약 시각(기본 19:00), 아니면 지금."""
    cfg = llm_window()
    when = when or now_kst()
    if when.tzinfo is None:
        when = when.replace(tzinfo=_tz())
    blocked, _ = in_blocked_window(when)
    if not blocked:
        return when
    sched = _parse_hhmm(cfg.get("scheduled_time", "19:00"))
    target = when.replace(hour=sched.hour, minute=sched.minute, second=0, microsecond=0)
    return target if target > when else target + timedelta(days=1)


def llm_window_guard(when: datetime | None = None, force: bool = False) -> GuardResult:
    blocked, why = in_blocked_window(when)
    nxt = next_allowed_llm_time(when)
    details = {"blocked": blocked, "reason": why, "next_allowed": nxt.isoformat()}
    if not blocked:
        return GuardResult("llm_window", True, f"실행 가능 ({why})", details)
    if force and llm_window().get("allow_force_flag", True):
        return GuardResult(
            "llm_window", True, f"--force 로 창 제한을 무시했습니다 ({why}).", {**details, "forced": True}
        )
    return GuardResult(
        "llm_window", False, f"{why} — evolve 를 {nxt:%Y-%m-%d %H:%M} KST 로 이월합니다.", details
    )
