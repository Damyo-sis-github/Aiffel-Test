"""일자별 상태 스냅샷 — 멱등성(#11)과 보충 실행 동일성(#32)의 토대.

`daily --date D` 는 D 를 처리하기 **전** 상태를 저장한다.
같은 D 를 다시 돌리면 그 스냅샷으로 되돌린 뒤 처리하므로 결과가 같다.
5일치 보충 실행과 5일 개별 실행이 같은 해시를 내는 이유도 이것이다.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

from app.paths import state_dir

SNAPSHOT_DIR = "snapshots"
TRACKED_FILES = ("paper_broker.json", "pending_orders.json", "predictions.json", "killswitch.json")


def snapshot_dir() -> Path:
    p = state_dir() / SNAPSHOT_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def snapshot_path(date: dt.date) -> Path:
    p = snapshot_dir() / date.isoformat()
    return p


def save_snapshot(date: dt.date) -> Path:
    """그 날짜를 처리하기 **전** 상태를 남긴다. 이미 있으면 덮어쓰지 않는다."""
    dst = snapshot_path(date)
    if dst.exists():
        return dst
    dst.mkdir(parents=True, exist_ok=True)
    for name in TRACKED_FILES:
        src = state_dir() / name
        if src.exists():
            shutil.copy2(src, dst / name)
    (dst / "_meta.json").write_text(
        json.dumps({"date": date.isoformat(), "files": sorted(f.name for f in dst.glob("*.json"))},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return dst


def restore_snapshot(date: dt.date) -> bool:
    """스냅샷이 있으면 되돌린다. 반환: 되돌렸는가(=재실행인가)."""
    src = snapshot_path(date)
    if not src.exists():
        return False
    for name in TRACKED_FILES:
        target = state_dir() / name
        candidate = src / name
        if candidate.exists():
            shutil.copy2(candidate, target)
        else:
            target.unlink(missing_ok=True)
    return True


def prepare(date: dt.date) -> bool:
    """처리 직전 호출. 재실행이면 True.

    과거 날짜를 다시 돌리면 그 이후 날짜의 스냅샷은 무효다.
    지우지 않으면 나중에 그 날짜를 재실행할 때 낡은 상태로 되돌아가 조용히 틀린 결과가 나온다.
    """
    if restore_snapshot(date):
        clear_snapshots_after(date)
        return True
    save_snapshot(date)
    return False


def clear_snapshots_after(date: dt.date) -> int:
    """되감기 실행 시 이후 날짜 스냅샷은 무효다."""
    n = 0
    for p in sorted(snapshot_dir().iterdir()):
        if p.is_dir() and p.name > date.isoformat():
            shutil.rmtree(p)
            n += 1
    return n
