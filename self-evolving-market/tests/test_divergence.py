"""§9 괴리 추적. 20거래 이동평균 괴리 > 0.3% → quarantine.

`divergence_quarantine_check` 는 처음부터 있었지만 **부르는 곳이 없었다.**
정의만 있고 아무도 안 부르는 함수는 구현이 아니다 — 리포트에는 계속
"페이퍼 시뮬만 운영 중 — 괴리 측정 대상 없음" 이 찍히고 있었다.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from app.data.meta_db import MetaDB
from app.evolve.curator import Curator
from app.execution import divergence as dv

SID = "s_drift"


def _trades(n: int, gap: float, sid: str = SID, base: float = 100.0) -> pd.DataFrame:
    """가정가 대비 `gap` 만큼 벌어진 체결 n건."""
    return pd.DataFrame([
        {"strategy_id": sid, "fill_date": (dt.date(2020, 1, 1) + dt.timedelta(days=i)).isoformat(),
         "fill_px": base * (1 + gap), "bt_px": base}
        for i in range(n)
    ])


# ------------------------------------------------------------ 판정
def test_no_gap_is_not_a_breach():
    gaps, bad, _ = dv.check(_trades(30, 0.0))
    assert bad == [] and gaps[SID] == pytest.approx(0.0)


def test_gap_over_threshold_is_a_breach():
    gaps, bad, summary = dv.check(_trades(30, 0.005))
    assert bad == [SID]
    assert gaps[SID] == pytest.approx(0.005)
    assert "0.500%" in summary and "격리 1건" in summary


def test_gap_just_under_threshold_is_not():
    """0.3% 는 한도다. 한도에 걸치는 값으로 격리하지 않는다."""
    _, bad, _ = dv.check(_trades(30, 0.0029))
    assert bad == []


def test_small_sample_is_not_a_pass(sandbox):
    """19건은 '이상 없음'이 아니라 '아직 모른다'다. 이걸 0 으로 채우면 거짓 안심이 된다."""
    gaps, bad, summary = dv.check(_trades(19, 0.05))
    assert bad == [], "표본이 모자란데 격리하면 안 된다"
    assert np.isnan(gaps[SID])
    assert "표본 부족" in summary and "이상 없음" not in summary


def test_only_the_last_20_trades_count():
    """이동평균이다. 오래된 큰 괴리가 영원히 따라다니면 안 된다."""
    old = _trades(20, 0.05)
    new = _trades(20, 0.0)
    new["fill_date"] = [(dt.date(2021, 1, 1) + dt.timedelta(days=i)).isoformat() for i in range(20)]
    _, bad, _ = dv.check(pd.concat([old, new], ignore_index=True))
    assert bad == []


def test_missing_bt_px_rows_are_skipped():
    """bt_px 가 없는 과거 행(컬럼 추가 전)이 계산을 오염시키면 안 된다."""
    df = _trades(20, 0.0)
    legacy = _trades(20, 0.0)
    legacy["bt_px"] = None
    gaps, bad, _ = dv.check(pd.concat([legacy, df], ignore_index=True))
    assert bad == [] and gaps[SID] == pytest.approx(0.0)


def test_each_strategy_is_judged_separately():
    both = pd.concat([_trades(30, 0.0, "clean"), _trades(30, 0.01, "dirty")], ignore_index=True)
    _, bad, _ = dv.check(both)
    assert bad == ["dirty"]


def test_empty_input_says_so():
    _, bad, summary = dv.check(pd.DataFrame())
    assert bad == [] and "측정 대상 거래 없음" in summary


# ------------------------------------------------------------ 격리 부작용
def test_quarantine_keeps_family(sandbox):
    """계열을 지우면 #9 계열 활성화 순서가 그 전략을 못 알아본다."""
    db = MetaDB()
    db.upsert("strategy_state", [{
        "strategy_id": SID, "family": "SHORT_US", "version": "1.2.0", "status": "active",
        "since": "2020-01-01", "allocation_pct": 0.3, "quarantined": 0, "note": "",
    }])
    tr = Curator(db).quarantine(SID, "§9 괴리 0.500%")
    row = db.query("SELECT * FROM strategy_state WHERE strategy_id = ?", (SID,)).iloc[0]
    assert row["family"] == "SHORT_US", "계열이 지워졌습니다"
    assert row["version"] == "1.2.0"
    assert int(row["quarantined"]) == 1
    assert float(row["allocation_pct"]) == 0.0
    assert tr.before == "active"


# ------------------------------------------------------------ 파이프라인 배선
def test_daily_records_the_assumed_price(seeded, locked):
    """bt_px 는 그날에만 계산할 수 있다 — 나중에 소급할 수 없다."""
    from app.pipeline.daily import DailyRunner

    r = DailyRunner(skip_guards=True)
    for d in (dt.date(2020, 12, 28), dt.date(2020, 12, 29)):
        r.run(d, mode="테스트")
    n = MetaDB().scalar("SELECT COUNT(*) FROM trades WHERE bt_px IS NOT NULL")
    assert n and int(n) > 0, "daily 가 백테스트 가정 체결가를 남기지 않았습니다"


def test_daily_report_carries_a_real_divergence_line(seeded, locked):
    """리포트가 '측정 대상 없음' 고정 문구를 벗어났는지."""
    from app.pipeline.daily import DailyRunner

    out = DailyRunner(skip_guards=True).run(dt.date(2020, 12, 29), mode="테스트")
    body = out.report_path.read_text(encoding="utf-8")
    assert "페이퍼 시뮬만 운영 중" not in body
    assert "백테스트 vs 시뮬 괴리" in body
