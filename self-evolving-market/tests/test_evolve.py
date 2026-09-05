"""#7 노이즈 추종 · #15 LLM 환각 · #27 리스크 우회 · #28 계열 순서 · #30 크레딧/실패 알림."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from app.evaluator.family_gates import family_activation_open
from app.evolve.curator import Curator
from app.evolve.similarity import (
    family_quota_ok,
    is_duplicate,
    retired_reproposal_ok,
    similarity,
)
from app.evolve.trigger import (
    CONSENSUS_NOTE,
    ROLLING_TRADES,
    evaluate_triggers,
    read_pending,
    write_pending,
)

TODAY = dt.date(2020, 6, 30)


def _trades(n: int, win_rate: float, expectancy: float, sid: str = "s1") -> pd.DataFrame:
    rng = np.random.default_rng(0)
    wins = int(n * win_rate)
    pnl = np.array([abs(expectancy) * 2] * wins + [-abs(expectancy)] * (n - wins))
    pnl = pnl - pnl.mean() + expectancy
    rng.shuffle(pnl)
    return pd.DataFrame({
        "strategy_id": sid, "account": "ACC_L", "closed": 1,
        "exit_date": [(TODAY - dt.timedelta(days=n - i)).isoformat() for i in range(n)],
        "pnl_pct": pnl,
    })


# ---------------------------------------------------------------- #7 노이즈 추종
def test_no_trigger_below_30_trades(sandbox):
    """n<30 승률 변화는 노이즈다. 절대 진화 트리거가 되면 안 된다."""
    bad = _trades(12, 0.20, -0.05)
    triggers = evaluate_triggers(today=TODAY, trades=bad, regime_row={}, )
    assert not [t for t in triggers if t.code == "T1"], "n<30 에서 T1 이 발동했습니다 (#7)"


def test_t1_fires_at_30_trades_with_bad_ci(sandbox):
    bad = _trades(ROLLING_TRADES, 0.20, -0.05)
    triggers = evaluate_triggers(today=TODAY, trades=bad, regime_row={})
    t1 = [t for t in triggers if t.code == "T1"]
    assert t1 and t1[0].target == "s1"


def test_t1_does_not_fire_for_healthy_strategy(sandbox):
    good = _trades(60, 0.85, 0.01)
    triggers = evaluate_triggers(today=TODAY, trades=good, regime_row={})
    assert not [t for t in triggers if t.code == "T1"]


def test_cooldown_blocks_repeat_evolution(sandbox):
    bad = _trades(ROLLING_TRADES, 0.20, -0.05)
    recent = {"s1": {"date": (TODAY - dt.timedelta(days=3)).isoformat()}}
    triggers = evaluate_triggers(today=TODAY, trades=bad, regime_row={}, last_cycles=recent)
    assert not [t for t in triggers if t.code == "T1"], "쿨다운 중인데 T1 이 발동했습니다"


def test_t2_requires_5_day_regime_confirmation(sandbox):
    row_early = {"regime": "bear", "regime_days": 2, "regime_confirmed": False}
    row_ok = {"regime": "bear", "regime_days": 5, "regime_confirmed": True}
    assert not [t for t in evaluate_triggers(today=TODAY, trades=pd.DataFrame(), regime_row=row_early)
                if t.code == "T2"]
    assert [t for t in evaluate_triggers(today=TODAY, trades=pd.DataFrame(), regime_row=row_ok)
            if t.code == "T2"]


def test_t0_is_warning_not_evolution(sandbox):
    triggers = evaluate_triggers(today=TODAY, trades=pd.DataFrame(), regime_row={}, pred_edge_ma=-0.05)
    t0 = [t for t in triggers if t.code == "T0"]
    assert t0 and not t0[0].is_evolution, "T0 는 진화 트리거가 아니다"


def test_pending_file_only_written_for_evolution_triggers(sandbox):
    only_t0 = evaluate_triggers(today=TODAY, trades=pd.DataFrame(), regime_row={}, pred_edge_ma=-0.05)
    assert write_pending(only_t0) is None
    assert read_pending() is None, "T0 만으로 진화 사이클이 열리면 안 된다 (크레딧 낭비)"

    with_t3 = evaluate_triggers(today=TODAY, trades=pd.DataFrame(), regime_row={},
                                is_first_trading_day_of_month=True)
    assert write_pending(with_t3) is not None
    st = read_pending()
    assert st and st.consensus == CONSENSUS_NOTE


def test_consensus_note_is_quotable(sandbox):
    assert "2026-09-03" in CONSENSUS_NOTE
    assert "트리거 조건" in CONSENSUS_NOTE


# ---------------------------------------------------------------- #10.4 유사도
def test_identical_hypothesis_is_duplicate():
    code = "def signals(f, d):\n    return f[f['rsi_2'] < 10]\n"
    dup, score, _ = is_duplicate(("RSI 과매도 반등", code), [("RSI 과매도 반등", code)])
    assert dup and score > 0.99


def test_renamed_variables_still_detected():
    a = "def signals(feats, date):\n    sel = feats[feats['rsi_2'] < 10]\n    return sel.head(5)\n"
    b = "def signals(x, t):\n    picked = x[x['rsi_2'] < 10]\n    return picked.head(5)\n"
    assert similarity("RSI 과매도", a, "RSI 과매도 반등", b) > 0.9


def test_different_hypothesis_is_not_duplicate():
    a = "def signals(f, d):\n    return f[f['rsi_2'] < 10]\n"
    b = "def signals(f, d):\n    return f[(f['mom_252'] > 0.2) & (f['vol_60'] < 0.3)].nlargest(3, 'mom_252')\n"
    dup, score, _ = is_duplicate(("모멘텀 상위", b), [("RSI 과매도", a)])
    assert not dup and score < 0.9


def test_family_quota_per_cycle():
    assert family_quota_ok("LONG", ["LONG", "ETF_ROT"])[0]
    assert not family_quota_ok("LONG", ["LONG", "LONG"])[0]


def test_retired_reproposal_requires_regime_change():
    assert not retired_reproposal_ok("bull", "bull")[0]
    assert retired_reproposal_ok("bull", "bear")[0]


# ---------------------------------------------------------------- #28 계열 순서
def test_gated_families_closed_without_long_active():
    for fam in ("SHORT_US", "LEV_ETF", "INV_ETF"):
        ok, why = family_activation_open(fam, {})
        assert not ok and "active 전략이" in why


def test_gated_families_open_with_long_active():
    for fam in ("SHORT_US", "LEV_ETF", "INV_ETF"):
        assert family_activation_open(fam, {"LONG": 1})[0]
    assert family_activation_open("SHORT_US", {"ETF_ROT": 2})[0]


def test_long_family_is_never_gated():
    assert family_activation_open("LONG", {})[0]
    assert family_activation_open("ETF_ROT", {})[0]


def test_curator_rejects_short_promotion_without_long(sandbox):
    from app.evaluator.gates import GateReport, GateResult

    curator = Curator()
    rep = GateReport(strategy_id="s_short", family="SHORT_US", k_index=1, alpha_k=0.05,
                     results=[GateResult("1", "기대값", True, 0.01, "> 0")])
    tr = curator.promote_to_candidate(rep)
    assert not tr.allowed and tr.after == "proposed"


def test_curator_allows_short_after_long_active(sandbox):
    from app.evaluator.gates import GateReport, GateResult

    curator = Curator()
    curator.set_state("s_long", "LONG", "1.0.0", "active")
    rep = GateReport(strategy_id="s_short", family="SHORT_US", k_index=1, alpha_k=0.05,
                     results=[GateResult("1", "기대값", True, 0.01, "> 0")])
    tr = curator.promote_to_candidate(rep)
    assert tr.allowed and tr.after == "candidate"


def test_curator_requires_30_paper_trades_for_active(sandbox):
    from app.evaluator.gates import GateReport, GateResult

    curator = Curator()
    rep = GateReport(strategy_id="s1", family="LONG", k_index=1, alpha_k=0.05,
                     results=[GateResult("1", "기대값", True, 0.01, "> 0")])
    curator.promote_to_candidate(rep)

    few = _trades(10, 0.7, 0.01)
    few["account"] = "ACC_L"
    tr = curator.promote_to_active("s1", few)
    assert not tr.allowed and "30" in tr.reason

    many = _trades(120, 0.62, 0.01)
    many["account"] = "ACC_L"
    tr2 = curator.promote_to_active("s1", many)
    assert tr2.allowed and tr2.after == "active"


def test_retired_state_is_kept_not_deleted(sandbox):
    curator = Curator()
    curator.set_state("s_old", "LONG", "1.0.0", "active")
    curator.retire("s_old", "OOS 성과 소멸", regime="bull")
    states = curator.states()
    assert "s_old" in set(states["strategy_id"]), "retired 전략 기록이 삭제되었습니다"
    assert states[states["strategy_id"] == "s_old"].iloc[0]["status"] == "retired"


def test_evolution_log_is_append_only(sandbox):
    from app.data.meta_db import MetaDB

    db = MetaDB()
    db.log_evolution(ts="2020-06-30T19:00:00", action="test", reason="최초")
    with pytest.raises(Exception, match="append-only"):
        db.query("UPDATE evolution_log SET reason = '조작' WHERE seq = 1")
    with pytest.raises(Exception, match="append-only"):
        db.query("DELETE FROM evolution_log WHERE seq = 1")


# ---------------------------------------------------------------- 배분
def test_allocation_respects_caps(sandbox):
    from app.portfolio.allocation import allocate

    ids = [f"s{i}" for i in range(5)]
    families = dict.fromkeys(ids, "LONG")
    w = allocate(ids, families, {i: 10.0 for i in ids}, days_running=300)
    assert sum(w.values()) == pytest.approx(1.0)
    assert max(w.values()) <= 0.40 + 1e-9


def test_allocation_family_cap(sandbox):
    from app.portfolio.allocation import allocate

    ids = ["a", "b", "c", "d"]
    families = {"a": "LONG", "b": "LONG", "c": "ETF_ROT", "d": "SHORT_US"}
    w = allocate(ids, families, {"a": 10, "b": 10, "c": 1, "d": 1}, days_running=300)
    long_total = w["a"] + w["b"]
    assert long_total <= 0.50 + 1e-6, f"계열 상한 50% 위반: {long_total}"


def test_allocation_equal_during_warmup(sandbox):
    from app.portfolio.allocation import allocate

    ids = ["a", "b", "c"]
    w = allocate(ids, dict.fromkeys(ids, "LONG"), {"a": 5, "b": 0, "c": 0}, days_running=10)
    assert all(abs(v - 1 / 3) < 1e-9 for v in w.values()), "워밍업 전에는 균등 배분이어야 한다"


# ---------------------------------------------------------------- #30 알림
def test_alert_failure_is_recorded_and_retried(sandbox, monkeypatch):
    from app.alerts.router import AlertRouter, Level
    from app.data.meta_db import MetaDB

    db = MetaDB()
    router = AlertRouter(db, cfg={"channels": {"telegram": {"enabled": True, "primary": True,
                                                            "levels": ["critical"], "max_lines": 10}},
                                  "offline_console_only": False, "retry": {"max_attempts": 5}})
    monkeypatch.setattr("app.alerts.router._send_telegram", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("실패")))
    router.send(Level.CRITICAL, "크레딧 소진")
    rows = db.query("SELECT * FROM alerts WHERE delivered = 0")
    assert len(rows) == 1 and "크레딧 소진" in rows.iloc[0]["message"]

    monkeypatch.setattr("app.alerts.router._send_telegram", lambda *a, **k: True)
    assert router.retry_failed() == 1
    assert db.query("SELECT * FROM alerts WHERE delivered = 0").empty


def test_telegram_summary_is_capped_at_10_lines(sandbox):
    from app.alerts.router import _truncate

    long_msg = "\n".join(f"line{i}" for i in range(30))
    out = _truncate(long_msg, 10)
    assert len(out.splitlines()) == 11        # 10줄 + 안내 1줄
    assert "+20줄" in out


# ---------------------------------------------------------------- 콘솔 전용 모드의 함정
CONSOLE_CFG = {
    "channels": {"telegram": {"enabled": True, "levels": ["critical", "info"], "max_lines": 10}},
    "offline_console_only": True,
    "console_counts_as_delivery_for": ["info"],
    "log_file": "alerts.log",
}


def test_console_mode_does_not_count_critical_as_delivered(sandbox):
    """콘솔 출력은 '전달'이 아니다.

    스케줄러로 돌면 stdout 이 어디에도 남지 않는다. critical 을 전달로 치면
    킬스위치·기기 승격 알림이 아무도 모르게 사라진다.
    """
    from app.alerts.router import AlertRouter, Level
    from app.data.meta_db import MetaDB

    db = MetaDB()
    router = AlertRouter(db, cfg=CONSOLE_CFG)
    router.send(Level.CRITICAL, "기기 레벨 승격 감지")
    router.send(Level.INFO, "일일 요약")

    pending = router.pending()
    assert len(pending) == 1
    assert "승격" in pending.iloc[0]["message"]
    # info 는 전달로 쳐서 대기열에 남지 않는다
    assert db.query("SELECT * FROM alerts WHERE level='info' AND delivered=1").shape[0] == 1


def test_alerts_always_written_to_log_file(sandbox):
    """채널과 무관하게 파일 로그는 항상 남는다 — 스케줄러 실행의 최후 보루."""
    from app.alerts.router import AlertRouter, Level
    from app.paths import reports_dir

    AlertRouter(cfg=CONSOLE_CFG).send(Level.CRITICAL, "킬스위치 발동")
    log = reports_dir() / "alerts.log"
    assert log.exists()
    assert "킬스위치 발동" in log.read_text(encoding="utf-8")
    assert "CRITICAL" in log.read_text(encoding="utf-8")


def test_console_mode_does_not_spam_retry(sandbox):
    """콘솔 전용 모드에서는 재전송하지 않는다 (매 실행마다 밀린 알림 전부 재출력 방지)."""
    from app.alerts.router import AlertRouter, Level

    router = AlertRouter(cfg=CONSOLE_CFG)
    router.send(Level.CRITICAL, "a")
    router.send(Level.CRITICAL, "b")
    assert router.retry_failed() == 0
    assert len(router.pending()) == 2      # 대기 상태로 남는다


def test_shipped_alert_config_warns_about_console_only(sandbox):
    from app.config import alerts_cfg

    cfg = alerts_cfg()
    assert cfg["offline_console_only"] is True
    assert cfg["console_counts_as_delivery_for"] == ["info"], (
        "critical 을 콘솔 전달로 인정하면 스케줄러 실행에서 조용히 사라집니다."
    )
