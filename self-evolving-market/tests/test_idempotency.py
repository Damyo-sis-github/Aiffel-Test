"""#11 상태 오염(멱등성) · #22 실행 누락일 · #24 정수 주식 왜곡 · #32 보충 실행.

핵심 명제 (§18): "노트북이 꺼져 있던 날은 결과에 영향이 없다. 영향이 있다면 룩어헤드 버그다."
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.data.meta_db import MetaDB
from app.pipeline.daily import DailyRunner
from app.util.calendars import trading_days

DAYS = [d for d in trading_days("US", dt.date(2020, 6, 1), dt.date(2020, 6, 12))]


@pytest.fixture()
def runner(seeded, locked):
    return DailyRunner(skip_guards=True)


def test_same_date_rerun_is_identical(runner):
    """#11 같은 날짜 daily 재실행 = 같은 결과."""
    d = DAYS[0]
    first = runner.run(d)
    assert first.ok
    second = DailyRunner(skip_guards=True).run(d)
    assert second.ok
    assert first.result_hash == second.result_hash, "같은 날짜 재실행이 다른 결과를 냅니다"


def test_sequence_is_deterministic(runner):
    hashes_a = [runner.run(d).result_hash for d in DAYS[:4]]
    # 상태를 지우고 처음부터 다시
    import shutil

    from app.paths import state_dir

    shutil.rmtree(state_dir(), ignore_errors=True)
    (state_dir() / "..").resolve()
    MetaDB().query("DELETE FROM trades")
    MetaDB().query("DELETE FROM nav")
    MetaDB().query("DELETE FROM run_log")
    hashes_b = [DailyRunner(skip_guards=True).run(d).result_hash for d in DAYS[:4]]
    assert hashes_a == hashes_b, "같은 순서 재실행이 다른 결과를 냅니다"


def test_catchup_matches_daily_runs(seeded, locked, tmp_path):
    """#22·#32 5일 보충 실행 vs 5일 개별 실행 → 결과 해시 동일."""
    import shutil

    from app.paths import state_dir

    days = DAYS[:5]

    # (A) 하루씩 실시간 실행
    r = DailyRunner(skip_guards=True)
    individual = [r.run(d, mode="실시간").result_hash for d in days]

    # (B) 상태 초기화 후 보충 실행
    shutil.rmtree(state_dir(), ignore_errors=True)
    db = MetaDB()
    for t in ("trades", "nav", "run_log", "predictions", "integrity_events"):
        db.query(f"DELETE FROM {t}")
    r2 = DailyRunner(skip_guards=True)
    catchup = [r2.run(d, mode="보충").result_hash for d in days]

    assert individual == catchup, (
        "보충 실행이 실시간 실행과 다른 결과를 냅니다 — 룩어헤드 또는 상태 오염 (#32)"
    )


def test_catchup_labels_are_recorded(runner):
    runner.run(DAYS[0], mode="보충")
    row = MetaDB().query("SELECT mode, catchup FROM run_log WHERE run_date = ?", (DAYS[0].isoformat(),))
    assert not row.empty
    assert str(row.iloc[0]["mode"]) == "보충"
    assert int(row.iloc[0]["catchup"]) == 1


def test_snapshot_restores_state(seeded, locked):
    from app.pipeline import state as snap

    d = DAYS[0]
    assert snap.prepare(d) is False       # 첫 실행
    assert snap.prepare(d) is True        # 재실행 → 스냅샷 복원


# ---------------------------------------------------------------- #24 정수 주식
def test_two_accounts_produce_nav_divergence_report(runner):
    for d in DAYS[:5]:
        runner.run(d)
    navs = MetaDB().query("SELECT date, account, nav FROM nav")
    assert set(navs["account"]) == {"ACC_S", "ACC_L"}, "두 계좌가 모두 기록되어야 한다 (#24)"
    wide = navs.pivot(index="date", columns="account", values="nav")
    rs = wide["ACC_S"] / wide["ACC_S"].iloc[0]
    rl = wide["ACC_L"] / wide["ACC_L"].iloc[0]
    # 정수 주식 제약 때문에 두 계좌 수익률은 완전히 같을 수 없다.
    assert not rs.equals(rl) or len(rs) < 2


def test_acc_s_rejects_sub_one_share_orders(seeded):
    """ACC_S 는 최소 1주 규칙 — 1주도 못 사면 주문을 폐기한다."""
    from app.backtest.costs import CostModel
    from app.portfolio.accounts import accounts

    acc = accounts()["ACC_S"]
    assert acc.capital_krw == 1_000_000
    assert acc.min_shares == 1
    frac_kr, _ = CostModel.from_config().fractional("KR")
    assert not frac_kr, "KR 은 정수 주식이어야 한다"
    frac_us, min_us = CostModel.from_config().fractional("US")
    assert frac_us and min_us == pytest.approx(0.01), "US 는 소수점 0.01주 허용"


def test_official_account_is_acc_l(seeded):
    from app.portfolio.accounts import official_account

    assert official_account() == "ACC_L"


# ---------------------------------------------------------------- replay
def test_replay_reproduces(runner):
    d = DAYS[0]
    first = runner.run(d)
    prev = MetaDB().query("SELECT result_hash FROM run_log WHERE run_date = ? AND task='daily'",
                          (d.isoformat(),))
    assert str(prev.iloc[0]["result_hash"]) == first.result_hash
    again = DailyRunner(skip_guards=True).run(d, mode="재현")
    assert again.result_hash == first.result_hash, "replay 재현 불가 = 버그 (§11.7)"


def test_rerunning_past_date_invalidates_later_snapshots(seeded, locked):
    """과거 날짜 재실행 → 그 이후 스냅샷은 무효가 되어야 한다.

    지우지 않으면 나중에 그 날짜를 재실행할 때 낡은 상태로 되돌아가 조용히 틀린 결과가 나온다.
    """
    from app.pipeline import state as snap

    r = DailyRunner(skip_guards=True)
    for d in DAYS[:3]:
        r.run(d)
    assert snap.snapshot_path(DAYS[2]).exists()

    r.run(DAYS[0])                      # 과거 날짜 재실행
    assert not snap.snapshot_path(DAYS[1]).exists()
    assert not snap.snapshot_path(DAYS[2]).exists()
