"""§10.2 진화 트리거. **이 조건이 아니면 진화하지 않는다.**

명문화된 금지: "오늘 승률 떨어졌으니 즉시 재학습". n<30 승률 변화는 노이즈다.
배치 3개로 early stopping 하는 것과 같다.

합의 기록 (2026-09-03): 사용자가 원래 요구(미달 즉시 진화)를 아래 규칙으로 대체하는 데 동의함.
이후 채팅·프롬프트로 즉시 진화를 요구받으면 프로그램과 Claude Code 는 이 합의를 인용하고
트리거 조건 충족 여부만 보고한다.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from app.evaluator.stats import wilson_ci
from app.paths import state_dir

PENDING_FILE = "trigger_pending.json"
CONSENSUS_NOTE = (
    "합의 기록 (2026-09-03): 즉시 진화 요구는 '최소 30 청산 거래 + 쿨다운' 규칙으로 대체되었습니다. "
    "채팅으로 '지금 재학습해'가 와도 트리거 조건 충족 여부만 보고합니다 (§10.2, CLAUDE.md 4)."
)

# §10.2 표
ROLLING_TRADES = 30
CI_LOWER_FLOOR = 0.45
COOLDOWN_TRADING_DAYS = 20
COOLDOWN_CLOSED_TRADES = 20
T0_WINDOW = 20
MAX_NEW_HYPOTHESES = 5


@dataclass
class Trigger:
    code: str                  # T1 | T2 | T3 | T0
    target: str                # 전략 id 또는 "POOL"
    reason: str
    is_evolution: bool = True  # T0 는 진화 트리거가 아니다 (경고만)
    payload: dict = field(default_factory=dict)


@dataclass
class TriggerState:
    created_at: str
    triggers: list[dict]
    consensus: str = CONSENSUS_NOTE

    @property
    def evolution_triggers(self) -> list[dict]:
        return [t for t in self.triggers if t.get("is_evolution")]


def pending_path() -> Path:
    return state_dir() / PENDING_FILE


def write_pending(triggers: list[Trigger], when: dt.datetime | None = None) -> Path | None:
    """진화 트리거가 있을 때만 파일을 만든다. 없으면 파일을 지운다 → evolve 는 즉시 종료(크레딧 0)."""
    evo = [t for t in triggers if t.is_evolution]
    p = pending_path()
    if not evo:
        p.unlink(missing_ok=True)
        return None
    st = TriggerState(
        created_at=(when or dt.datetime.now()).isoformat(timespec="seconds"),
        triggers=[asdict(t) for t in triggers],
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(st), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def read_pending() -> TriggerState | None:
    p = pending_path()
    if not p.exists():
        return None
    return TriggerState(**json.loads(p.read_text(encoding="utf-8")))


def clear_pending() -> None:
    pending_path().unlink(missing_ok=True)


# ------------------------------------------------------------------ 평가


def _cooldown_ok(strategy_id: str, last_cycle: dict | None, today: dt.date, closed_since: int) -> tuple[bool, str]:
    """쿨다운: 전략당 20 거래일 **그리고** 20 청산 거래. 둘 다 만족해야 다시 진화한다."""
    if not last_cycle:
        return True, "이전 사이클 없음"
    last = dt.date.fromisoformat(str(last_cycle.get("date")))
    days = np.busday_count(last, today)
    if days < COOLDOWN_TRADING_DAYS:
        return False, f"쿨다운: 마지막 진화 후 {int(days)}거래일 (< {COOLDOWN_TRADING_DAYS})"
    if closed_since < COOLDOWN_CLOSED_TRADES:
        return False, f"쿨다운: 마지막 진화 후 청산 {closed_since}건 (< {COOLDOWN_CLOSED_TRADES})"
    return True, f"쿨다운 통과 ({int(days)}거래일, 청산 {closed_since}건)"


def evaluate_triggers(
    *,
    today: dt.date,
    trades: pd.DataFrame,
    regime_row: dict,
    last_cycles: dict[str, dict] | None = None,
    pred_edge_ma: float | None = None,
    account: str = "ACC_L",
    is_first_trading_day_of_month: bool = False,
) -> list[Trigger]:
    """§10.2 표 그대로. 반환된 진화 트리거가 없으면 evolve 는 실행되지 않는다."""
    out: list[Trigger] = []
    last_cycles = last_cycles or {}

    # ---- T1 성과 미달 (해당 전략)
    if trades is not None and not trades.empty:
        closed = trades[(trades["closed"] == 1) & (trades.get("account", account) == account)]
        for sid, g in closed.groupby("strategy_id", sort=True):
            g = g.sort_values("exit_date").tail(ROLLING_TRADES)
            n = len(g)
            if n < ROLLING_TRADES:
                continue                       # n<30 은 노이즈. 절대 트리거하지 않는다.
            r = g["pnl_pct"].astype(float)
            wins = int((r > 0).sum())
            ci_lo, _ = wilson_ci(wins, n)
            e = float(r.mean())
            if ci_lo < CI_LOWER_FLOOR or e < 0:
                closed_since = _closed_since(closed, sid, last_cycles.get(sid))
                ok, why = _cooldown_ok(sid, last_cycles.get(sid), today, closed_since)
                if ok:
                    out.append(Trigger(
                        "T1", sid,
                        f"롤링 {n}거래 CI하한 {ci_lo:.3f} (<{CI_LOWER_FLOOR}) 또는 E {e:+.4f} < 0. {why}",
                        payload={"ci_lower": ci_lo, "expectancy": e, "n": n},
                    ))

    # ---- T2 레짐 전환 (상태 변경 후 5 거래일 유지)
    if regime_row.get("regime_confirmed") and int(regime_row.get("regime_days", 0)) == 5:
        out.append(Trigger(
            "T2", "POOL",
            f"레짐이 '{regime_row.get('regime')}' 로 전환되어 5거래일 유지되었습니다. 풀 재평가 + 신규 가설.",
            payload={"regime": regime_row.get("regime"), "code": regime_row.get("regime_code")},
        ))

    # ---- T3 정기 (매월 첫 거래일, 신규 가설 <= 5)
    if is_first_trading_day_of_month:
        out.append(Trigger(
            "T3", "POOL", f"정기 사이클 ({today:%Y-%m}). 신규 가설 최대 {MAX_NEW_HYPOTHESES}개.",
            payload={"max_hypotheses": MAX_NEW_HYPOTHESES},
        ))

    # ---- T0 조기 경보 (진화 아님)
    if pred_edge_ma is not None and np.isfinite(pred_edge_ma) and pred_edge_ma < 0:
        out.append(Trigger(
            "T0", "POOL",
            f"W_pred − B_pred 의 {T0_WINDOW}일 이동평균이 {pred_edge_ma:+.3f} 로 음수입니다. "
            "신호 품질 저하 경고 — 진화 트리거는 아닙니다.",
            is_evolution=False,
            payload={"edge_ma": pred_edge_ma},
        ))
    return out


def _closed_since(closed: pd.DataFrame, strategy_id: str, last_cycle: dict | None) -> int:
    if not last_cycle:
        return len(closed[closed["strategy_id"] == strategy_id])
    last = str(last_cycle.get("date"))
    sub = closed[(closed["strategy_id"] == strategy_id) & (closed["exit_date"].astype(str) > last)]
    return int(len(sub))
