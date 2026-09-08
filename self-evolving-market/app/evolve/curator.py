"""§10.3-7 Curator — 상태 전이 + 배분. **규칙 코드다. LLM 이 아니다.**

상태 전이: proposed → candidate → active → retired
  proposed  : Generator 가 낸 가설. 정적 검사·dry-run 통과 전.
  candidate : 워크포워드 + §7.5/§7.6 게이트 통과. 아직 실거래 통계 없음.
  active    : ACC_L 페이퍼 30 청산 거래 후 게이트 1·2 재통과.
  retired   : 은퇴. **코드·로그를 삭제하지 않는다.**
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.config import gates as load_gates
from app.data.meta_db import MetaDB
from app.evaluator.family_gates import family_activation_open
from app.evaluator.gates import GateReport
from app.evaluator.stats import wilson_ci
from app.portfolio.allocation import allocate

STATUSES = ("proposed", "candidate", "active", "retired")
PROMOTION_TRADES = 30


@dataclass
class Transition:
    strategy_id: str
    before: str
    after: str
    reason: str
    allowed: bool = True


class Curator:
    def __init__(self, db: MetaDB | None = None):
        self.db = db or MetaDB()
        self.cfg = load_gates()

    # ------------------------------------------------------------ 상태

    def states(self) -> pd.DataFrame:
        return self.db.query("SELECT * FROM strategy_state ORDER BY strategy_id")

    def active_by_family(self) -> dict[str, int]:
        df = self.states()
        if df.empty:
            return {}
        act = df[df["status"] == "active"]
        return act.groupby("family").size().to_dict()

    def open_families(self) -> dict[str, bool]:
        by_family = self.active_by_family()
        gated = (self.cfg.get("family_activation_order") or {}).get("gated") or []
        prereq = (self.cfg.get("family_activation_order") or {}).get("gating_prerequisite") or []
        out = {f: True for f in prereq}
        for f in gated:
            out[f] = family_activation_open(f, by_family)[0]
        return out

    def set_state(self, strategy_id: str, family: str, version: str, status: str, note: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(f"알 수 없는 상태: {status}")
        self.db.upsert(
            "strategy_state",
            [{"strategy_id": strategy_id, "family": family, "version": version, "status": status,
              "since": dt.date.today().isoformat(), "allocation_pct": 0.0, "quarantined": 0, "note": note}],
        )

    # ------------------------------------------------------------ 전이

    def promote_to_candidate(self, report: GateReport, version: str = "1.0.0") -> Transition:
        """워크포워드 게이트 통과 → candidate. 계열 순서를 여기서 강제한다 (#28)."""
        ok_family, why = family_activation_open(report.family, self.active_by_family())
        if not ok_family:
            return Transition(report.strategy_id, "proposed", "proposed", why, allowed=False)
        if not report.passed:
            fails = ", ".join(f"게이트{r.number}" for r in report.failures)
            return Transition(report.strategy_id, "proposed", "proposed",
                              f"게이트 실패: {fails}", allowed=False)
        self.set_state(report.strategy_id, report.family, version, "candidate", why)
        return Transition(report.strategy_id, "proposed", "candidate", f"게이트 전부 통과. {why}")

    def promote_to_active(self, strategy_id: str, paper_trades: pd.DataFrame, account: str = "ACC_L") -> Transition:
        """§7.6 candidate → active: ACC_L 페이퍼 30 청산 거래 후 게이트 1·2 재통과."""
        row = self.db.query("SELECT * FROM strategy_state WHERE strategy_id = ?", (strategy_id,))
        if row.empty or row.iloc[0]["status"] != "candidate":
            return Transition(strategy_id, "?", "?", "candidate 상태가 아닙니다.", allowed=False)
        family = str(row.iloc[0]["family"])

        ok_family, why_family = family_activation_open(family, self.active_by_family())
        if not ok_family:
            return Transition(strategy_id, "candidate", "candidate", why_family, allowed=False)

        sub = paper_trades[
            (paper_trades["strategy_id"] == strategy_id)
            & (paper_trades["closed"] == 1)
            & (paper_trades.get("account", account) == account)
        ]
        n = len(sub)
        if n < PROMOTION_TRADES:
            return Transition(strategy_id, "candidate", "candidate",
                              f"ACC_L 청산 {n}건 (< {PROMOTION_TRADES}). 아직 승격 조건 미달.", allowed=False)

        r = sub["pnl_pct"].astype(float)
        e = float(r.mean())
        w = float((r > 0).mean())
        ci_lo, _ = wilson_ci(int((r > 0).sum()), n)
        c = self.cfg["common"]
        ok = (
            e > float(c["expectancy_min"])
            and w >= float(c["win_rate_min"])
            and ci_lo >= float(c["win_rate_ci_lower_min"])
        )
        detail = f"n={n}, E={e:+.4f}, W={w:.3f}, CI하한={ci_lo:.3f}"
        if not ok:
            return Transition(strategy_id, "candidate", "candidate",
                              f"게이트 1·2 재통과 실패 ({detail})", allowed=False)
        self.set_state(strategy_id, family, str(row.iloc[0]["version"]), "active", detail)
        return Transition(strategy_id, "candidate", "active", f"게이트 1·2 재통과 ({detail}). {why_family}")

    def retire(self, strategy_id: str, reason: str, regime: str = "") -> Transition:
        """은퇴. 코드와 로그는 남는다 (§7.6). 재제안은 레짐이 다를 때만 (§10.4)."""
        row = self.db.query("SELECT * FROM strategy_state WHERE strategy_id = ?", (strategy_id,))
        before = str(row.iloc[0]["status"]) if not row.empty else "unknown"
        family = str(row.iloc[0]["family"]) if not row.empty else ""
        version = str(row.iloc[0]["version"]) if not row.empty else "1.0.0"
        self.set_state(strategy_id, family, version, "retired", f"{reason} | 은퇴 당시 레짐: {regime}")
        return Transition(strategy_id, before, "retired", reason)

    def quarantine(self, strategy_id: str, reason: str) -> Transition:
        """§9 괴리 추적: 20거래 이동평균 괴리 > 0.3% → quarantine.

        family·version 은 **기존 값을 지킨다.** 빈 문자열로 upsert 하면 계열이 지워지고,
        계열 게이트(#9 SHORT/LEV/INV 활성화 순서)가 그 전략을 못 알아본다.
        """
        row = self.db.query(
            "SELECT family, version, status FROM strategy_state WHERE strategy_id = ?", (strategy_id,)
        )
        before = str(row.iloc[0]["status"]) if not row.empty else "active"
        self.db.upsert("strategy_state", [{
            "strategy_id": strategy_id,
            "family": str(row.iloc[0]["family"]) if not row.empty else "",
            "version": str(row.iloc[0]["version"]) if not row.empty else "1.0.0",
            "status": before, "since": dt.date.today().isoformat(),
            "allocation_pct": 0.0, "quarantined": 1, "note": reason,
        }])
        return Transition(strategy_id, before, "quarantined", reason)

    # ------------------------------------------------------------ 배분

    def rebalance(self, sharpe_126: dict[str, float] | None = None, days_running: int | None = None) -> dict[str, float]:
        df = self.states()
        if df.empty:
            return {}
        act = df[(df["status"] == "active") & (df["quarantined"] == 0)]
        if act.empty:
            return {}
        ids = list(act["strategy_id"])
        families = dict(zip(act["strategy_id"], act["family"], strict=True))
        weights = allocate(ids, families, sharpe_126, days_running=days_running)
        self.db.upsert(
            "strategy_state",
            [
                {**{k: r[k] for k in ("strategy_id", "family", "version", "status", "since", "quarantined", "note")},
                 "allocation_pct": float(weights.get(r["strategy_id"], 0.0))}
                for _, r in act.iterrows()
            ],
        )
        return weights


def divergence_quarantine_check(
    backtest_px: pd.Series, live_px: pd.Series, *, window: int = 20, threshold: float = 0.003
) -> tuple[bool, float]:
    """§9 괴리 추적. 반환 (격리 필요 여부, 이동평균 괴리)."""
    common = backtest_px.index.intersection(live_px.index)
    if len(common) < window:
        return False, float("nan")
    diff = (live_px[common] - backtest_px[common]).abs() / backtest_px[common].replace(0, np.nan)
    ma = float(diff.tail(window).mean())
    return (np.isfinite(ma) and ma > threshold), ma
