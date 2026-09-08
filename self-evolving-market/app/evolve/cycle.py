"""진화 사이클의 시작과 끝을 기록한다.

여기가 없으면 **쿨다운이 영원히 작동하지 않는다.**

§10.2 의 쿨다운(마지막 진화 후 20 거래일 그리고 청산 20건)은
`evolution_log` 의 `action='cycle_complete'` 행을 읽어서 판정한다
(`DailyRunner._last_cycles`). 그런데 그 행을 쓰는 코드가 없었다.
결과: T1 이 한 번 켜지면 매일 밤 `evolve` 가 다시 돌고, 크레딧을 계속 태우고,
n<30 노이즈로 재학습을 반복한다 — 합의(2026-09-03)로 없애기로 한 바로 그 동작이다.

명세 §10 의 `/evolve` 절차는 "마지막에 evolution_log 에 남긴다"고만 적혀 있고,
그걸 할 **명령이 없었다.** LLM 이 파이썬을 직접 쓰지 않는 한 지킬 수 없는 규칙이었다.
그래서 (1) `quant cycle-complete` 라는 결정론 명령을 만들고,
(2) LLM 이 그걸 부르지 않고 끝내도 `cmd_evolve` 가 대신 찍는다.
"""

from __future__ import annotations

import datetime as dt
import hashlib

from app.data.meta_db import MetaDB
from app.evolve.trigger import pending_path

ACTION = "cycle_complete"


def new_cycle_id(date: dt.date, targets: list[str]) -> str:
    """같은 날 같은 대상이면 같은 id. 재실행이 사이클을 늘리지 않는다 (#11 멱등성)."""
    digest = hashlib.sha256("|".join(sorted(targets)).encode()).hexdigest()[:6]
    return f"{date:%Y%m%d}-{digest}"


def is_complete(cycle_id: str, db: MetaDB | None = None) -> bool:
    db = db or MetaDB()
    row = db.query(
        "SELECT 1 FROM evolution_log WHERE action = ? AND cycle_id = ? LIMIT 1",
        (ACTION, cycle_id),
    )
    return not row.empty


def complete(
    cycle_id: str,
    targets: list[str],
    *,
    trigger: str | None = None,
    reason: str = "",
    db: MetaDB | None = None,
    when: dt.datetime | None = None,
    clear_pending: bool = True,
) -> bool:
    """사이클 종료를 찍는다. 이미 찍혀 있으면 아무것도 하지 않고 False.

    `targets` 가 여기서 쿨다운의 적용 대상이 된다. `_last_cycles` 가
    `after["targets"]` 를 그대로 읽는다 — 키 이름을 바꾸면 쿨다운이 조용히 죽는다.
    """
    db = db or MetaDB()
    if is_complete(cycle_id, db):
        return False
    db.log_evolution(
        ts=(when or dt.datetime.now()).isoformat(timespec="seconds"),
        action=ACTION,
        cycle_id=cycle_id,
        trigger=trigger,
        after={"targets": sorted(set(targets))},
        reason=reason,
    )
    if clear_pending:
        # 지우지 않아도 daily 가 매일 다시 쓰지만, 쿨다운이 걸린 상태에서
        # 낡은 파일이 남아 있으면 evolve 가 헛돈다.
        pending_path().unlink(missing_ok=True)
    return True


def targets_of(triggers: list[dict]) -> list[str]:
    """진화 트리거의 대상만 뽑는다. POOL 은 특정 전략이 아니므로 쿨다운 대상이 아니다."""
    return sorted({
        str(t.get("target"))
        for t in triggers
        if t.get("is_evolution") and t.get("target") and t.get("target") != "POOL"
    })
