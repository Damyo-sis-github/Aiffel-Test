"""§10.2 쿨다운이 실제로 걸리는지.

쿨다운 판정 코드는 처음부터 있었지만 **`cycle_complete` 를 쓰는 코드가 없었다.**
그래서 T1 이 한 번 켜지면 매일 밤 evolve 가 다시 돌았다. 읽는 쪽만 테스트하면
이 구멍이 안 보인다 — 쓰는 쪽과 읽는 쪽을 이어서 본다.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from app.data.meta_db import MetaDB
from app.evolve.cycle import complete, is_complete, new_cycle_id, targets_of
from app.evolve.trigger import evaluate_triggers, pending_path
from app.pipeline.daily import DailyRunner

SID = "h20_sector_rs"
TODAY = dt.date(2020, 12, 31)


def _losing_trades(n: int = 30, first: dt.date = dt.date(2020, 11, 2)) -> pd.DataFrame:
    """CI 하한이 0.45 를 밑도는 30 청산 거래. T1 이 켜질 조건."""
    return pd.DataFrame([
        {"strategy_id": SID, "account": "ACC_L", "closed": 1,
         "exit_date": (first + dt.timedelta(days=i)).isoformat(),
         "pnl_pct": -0.01 if i % 3 else 0.005}
        for i in range(n)
    ])


def _triggers(last_cycles: dict) -> list:
    return evaluate_triggers(
        today=TODAY, trades=_losing_trades(), regime_row={}, last_cycles=last_cycles,
    )


# ------------------------------------------------------------ 읽는 쪽 ↔ 쓰는 쪽
def test_t1_fires_when_no_cycle_recorded(sandbox):
    assert [t.code for t in _triggers({})] == ["T1"]


def test_recorded_cycle_suppresses_t1(sandbox):
    """사이클을 찍으면 같은 전략의 T1 이 멈춘다. 이게 쿨다운의 전부다."""
    cid = new_cycle_id(TODAY, [SID])
    assert complete(cid, [SID], trigger="T1", reason="테스트")
    last = DailyRunner(skip_guards=True)._last_cycles()
    assert SID in last, "cycle_complete 를 썼는데 _last_cycles 가 못 읽는다 — 키 이름이 어긋났다"
    assert _triggers(last) == []


def test_cycle_complete_is_idempotent(sandbox):
    """같은 날 재실행이 사이클을 두 번 세면 안 된다 (#11)."""
    cid = new_cycle_id(TODAY, [SID])
    assert complete(cid, [SID]) is True
    assert complete(cid, [SID]) is False
    rows = MetaDB().query("SELECT 1 FROM evolution_log WHERE action = 'cycle_complete'")
    assert len(rows) == 1


def test_cycle_id_is_stable_for_same_day_and_targets(sandbox):
    assert new_cycle_id(TODAY, ["b", "a"]) == new_cycle_id(TODAY, ["a", "b"])
    assert new_cycle_id(TODAY, ["a"]) != new_cycle_id(TODAY, ["a", "b"])


def test_completing_clears_pending_file(sandbox):
    p = pending_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")
    complete(new_cycle_id(TODAY, [SID]), [SID])
    assert not p.exists(), "trigger_pending 이 남으면 evolve 가 헛돈다"


def test_is_complete_reports_state(sandbox):
    cid = new_cycle_id(TODAY, [SID])
    assert not is_complete(cid)
    complete(cid, [SID])
    assert is_complete(cid)


# ------------------------------------------------------------ 백스톱
def test_evolve_stamps_the_cycle_even_if_the_llm_forgets(sandbox, monkeypatch):
    """LLM 이 cycle-complete 를 안 불러도 evolve 가 대신 찍는다.

    안 찍으면 같은 트리거가 매일 밤 evolve 를 깨워 크레딧을 계속 태운다.
    이건 LLM 의 성실성에 기댈 수 없는 종류의 일이다.
    """
    import subprocess

    from app import cli
    from app.evolve.trigger import Trigger, write_pending

    write_pending([Trigger("T1", SID, "테스트")])

    class _Done:
        returncode = 0

    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    args = cli.build_parser().parse_args(["evolve", "--if-pending", "--force"])
    assert args.func(args) == 0

    # 사이클 id 는 우리가 정해서 LLM 에 넘긴다 (LLM 이 지어내면 K 와 쿨다운이 어긋난다)
    cid = new_cycle_id(dt.date.today(), [SID])
    assert f"/evolve {cid}" in seen["cmd"]
    assert is_complete(cid), "evolve 가 끝났는데 쿨다운이 시작되지 않았다"
    assert not pending_path().exists()


# ------------------------------------------------------------ 대상 추출
def test_pool_is_not_a_cooldown_target():
    """T2/T3 의 대상은 POOL 이다. 특정 전략이 아니므로 쿨다운을 걸 수 없다."""
    triggers = [
        {"code": "T1", "target": SID, "is_evolution": True},
        {"code": "T2", "target": "POOL", "is_evolution": True},
        {"code": "T0", "target": "other", "is_evolution": False},
    ]
    assert targets_of(triggers) == [SID]
