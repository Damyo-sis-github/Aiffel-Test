"""§10.3 컨텍스트 팩 — `/evolve` 가 읽는 **유일한** 입력.

이게 없으면 LLM 은 성과·레짐·랭킹·누적 K·계열 게이트·실패 진단을 하나도 못 본 채
가설을 만든다. 그런데 `write_context_pack` 은 **부르는 곳이 없었고**,
`diagnose()` 는 실제 trades 표에 없는 컬럼(gross_pnl, hold_days)을 기대해
KeyError 로 죽었다. 둘 다 한 번도 실행된 적이 없어서 테스트가 전부 통과했다.
"""

from __future__ import annotations

import datetime as dt
import json

import pandas as pd

from app.evolve.context_pack import diagnose

REAL_COLUMNS = [
    "trade_id", "account", "strategy_id", "symbol", "market", "side", "instrument",
    "family", "signal_date", "fill_date", "fill_px", "exit_date", "exit_px", "qty",
    "cost", "borrow_cost", "pnl", "pnl_pct", "closed", "exit_reason", "bt_px",
]


def _trades(n: int = 12, **over) -> pd.DataFrame:
    """meta_db 의 trades 스키마와 **같은 컬럼**으로 만든다. 여기서 어긋나면 의미가 없다."""
    base = {
        "trade_id": [f"t{i}" for i in range(n)], "account": "ACC_L", "strategy_id": "s1",
        "symbol": "AAPL", "market": "US", "side": 1, "instrument": "stock", "family": "LONG",
        "signal_date": "2020-06-01", "fill_date": "2020-06-02", "fill_px": 100.0,
        "exit_date": "2020-06-12", "exit_px": 101.0, "qty": 10.0,
        "cost": 1.0, "borrow_cost": 0.0, "pnl": 10.0, "pnl_pct": 0.01,
        "closed": 1, "exit_reason": "horizon", "bt_px": 100.0,
    }
    base.update(over)
    return pd.DataFrame(base, index=range(n))[REAL_COLUMNS]


def test_diagnose_runs_on_the_real_trades_schema():
    """상상한 스키마가 아니라 DB 가 실제로 주는 컬럼으로 돈다."""
    out = diagnose(_trades())
    assert out and isinstance(out[0], str)


def test_diagnose_derives_gross_pnl_from_net_and_costs():
    """비용이 총손익의 절반을 넘으면 '비용이 먹는다'로 진단해야 한다."""
    # net=1, cost=5 → gross = 1+5 = 6, cost/gross = 0.83 > 0.5
    out = diagnose(_trades(pnl=1.0, cost=5.0))
    assert any("비용" in s for s in out), out


def test_diagnose_does_not_flag_cost_when_costs_are_small():
    out = diagnose(_trades(pnl=100.0, cost=1.0))
    assert not any("비용" in s for s in out), out


def test_diagnose_derives_hold_days_for_leveraged_decay():
    """LEV/INV 는 보유일수와 손익의 상관을 본다. hold_days 가 없으면 이 진단이 죽는다."""
    n = 12
    df = _trades(n, instrument="lev_etf")
    df["exit_date"] = [f"2020-06-{2 + i:02d}" for i in range(n)]   # 보유일 증가
    df["pnl_pct"] = [0.05 - 0.01 * i for i in range(n)]            # 손익 감소
    out = diagnose(df)
    assert isinstance(out, list) and out


def test_diagnose_handles_empty_and_unclosed():
    assert "거래 없음" in diagnose(pd.DataFrame())[0]
    assert "청산 거래 없음" in diagnose(_trades(closed=0))[0]


# ------------------------------------------------------------ 배선
def test_daily_writes_the_context_pack_when_a_trigger_fires(seeded, locked):
    """트리거가 떴는데 팩이 없으면 /evolve 가 빈손으로 시작한다."""
    from app.evolve.trigger import Trigger, write_pending
    from app.paths import state_dir
    from app.pipeline.daily import DailyRunner

    r = DailyRunner(skip_guards=True)
    for d in (dt.date(2020, 12, 30), dt.date(2020, 12, 31)):
        r.run(d, mode="테스트")

    # T3 가 뜨는 날이 아닐 수 있으므로 트리거를 직접 넣고 저장 경로만 검증한다.
    write_pending([Trigger("T3", "POOL", "테스트")])
    from app.data.pit_store import PITStore
    from app.features.builder import FeatureBuilder

    panel = FeatureBuilder(PITStore()).build(dt.date(2020, 12, 31),
                                             start=dt.date(2019, 12, 1))
    msg = r._save_context_pack(dt.date(2020, 12, 31), [Trigger("T3", "POOL", "테스트")],
                               r.db.query("SELECT * FROM trades"), panel, [])
    assert msg is None, msg
    pack = json.loads((state_dir() / "context_pack.json").read_text(encoding="utf-8"))
    for key in ("triggers", "performance", "regime_matrix", "rankings",
                "multiple_testing", "family_gate", "failure_diagnoses", "rules_reminder"):
        assert key in pack, key
    assert pack["multiple_testing"]["alpha_k"] > 0


def test_proposed_packages_exist_and_are_importable():
    """`/evolve` 가 쓸 수 있는 두 디렉터리. 패키지가 아니면 registry 가 조용히 무시한다."""
    import app.features.proposed
    import app.strategies.proposed
    from app.strategies.registry import all_strategies

    assert app.strategies.proposed.__path__
    assert app.features.proposed.__path__
    assert all_strategies()          # 시드는 계속 잡혀야 한다
