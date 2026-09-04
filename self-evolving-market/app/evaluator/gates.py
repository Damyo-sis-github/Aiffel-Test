"""§7.5 공통 채택 게이트 8종.

게이트 실패는 실패로 보고한다. 돌려 말하지 않고 기준을 낮추지 않는다 (CLAUDE.md 6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.config import gates as load_gates
from app.evaluator.family_gates import evaluate_family
from app.evaluator.stats import alpha_for_k, bootstrap_p_value, wilson_ci


@dataclass(frozen=True)
class GateResult:
    number: str
    name: str
    passed: bool
    observed: Any
    threshold: Any
    detail: str = ""

    def __str__(self) -> str:
        mark = "통과" if self.passed else "실패"
        return f"  [{self.number}] {self.name}: {mark} (관측 {self.observed}, 기준 {self.threshold}){' — ' + self.detail if self.detail else ''}"


@dataclass
class GateReport:
    strategy_id: str
    family: str
    k_index: int
    alpha_k: float
    results: list[GateResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[GateResult]:
        return [r for r in self.results if not r.passed]

    def to_dict(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "family": self.family,
            "k_index": self.k_index,
            "alpha_k": self.alpha_k,
            "passed": self.passed,
            "results": [
                {"number": r.number, "name": r.name, "passed": r.passed,
                 "observed": r.observed, "threshold": r.threshold, "detail": r.detail}
                for r in self.results
            ],
        }

    def summary(self) -> str:
        head = f"[{self.strategy_id}] 게이트 {'전부 통과' if self.passed else '실패'} (K={self.k_index}, α_K={self.alpha_k:.4f})"
        return head + "\n" + "\n".join(str(r) for r in self.results)


def _fmt(x) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "n/a"
    return f"{x:.4f}" if isinstance(x, float) else str(x)


def evaluate(
    metrics: dict,
    *,
    strategy_id: str,
    family: str,
    horizon_days: int,
    k_index: int,
    control_expectancies: list[float] | None = None,
    fold_expectancies: list[float] | None = None,
    is_sharpe: float | None = None,
    oos_sharpe: float | None = None,
    family_context: dict | None = None,
) -> GateReport:
    """§7.5 공통 8종 + §7.6 계열별 추가 게이트."""
    cfg = load_gates()
    c = cfg["common"]
    alpha_k = alpha_for_k(float(c["control_base_alpha"]), k_index)
    rep = GateReport(strategy_id=strategy_id, family=family, k_index=k_index, alpha_k=alpha_k)

    n_closed = int(metrics.get("n_closed", 0) or 0)
    expectancy = float(metrics.get("expectancy", np.nan))
    win_rate = float(metrics.get("win_rate", np.nan))
    sharpe = float(metrics.get("sharpe", np.nan))
    mdd = float(metrics.get("mdd", np.nan))

    # 1. 기대값 E > 0  ← 1차 게이트
    rep.results.append(
        GateResult("1", "기대값 E > 0", bool(np.isfinite(expectancy) and expectancy > float(c["expectancy_min"])),
                   _fmt(expectancy), f"> {c['expectancy_min']}",
                   "승률이 높아도 손익비가 나쁘면 여기서 걸린다.")
    )

    # 2. 승률: W >= 50%, 95% CI 하한 >= 45%, n >= 30
    wins = int(round(win_rate * n_closed)) if np.isfinite(win_rate) else 0
    ci_lo, ci_hi = wilson_ci(wins, n_closed) if n_closed else (np.nan, np.nan)
    ok2 = (
        n_closed >= int(c["min_closed_trades"])
        and np.isfinite(win_rate) and win_rate >= float(c["win_rate_min"])
        and np.isfinite(ci_lo) and ci_lo >= float(c["win_rate_ci_lower_min"])
    )
    rep.results.append(
        GateResult("2", "거래 승률", ok2,
                   f"W={_fmt(win_rate)} n={n_closed} CI=[{_fmt(ci_lo)}, {_fmt(ci_hi)}]",
                   f">= {c['win_rate_min']}, CI하한 >= {c['win_rate_ci_lower_min']}, n >= {c['min_closed_trades']}")
    )

    # 3. 샤프 (h<=1 은 더 높은 기준)
    thr3 = float(c["sharpe_min_short_horizon"]) if horizon_days <= int(c["short_horizon_days"]) else float(c["sharpe_min"])
    rep.results.append(
        GateResult("3", "샤프", bool(np.isfinite(sharpe) and sharpe >= thr3), _fmt(sharpe), f">= {thr3}")
    )

    # 4. MDD
    rep.results.append(
        GateResult("4", "MDD", bool(np.isfinite(mdd) and mdd <= float(c["mdd_max"])), _fmt(mdd), f"<= {c['mdd_max']}")
    )

    # 5. 대조군 부트스트랩 p < α_K
    ctrl = list(control_expectancies or [])
    p = bootstrap_p_value(expectancy, ctrl, iters=int(c["control_bootstrap_iters"])) if ctrl else 1.0
    rep.results.append(
        GateResult("5", "무작위 대조군", bool(ctrl and p < alpha_k), f"p={_fmt(p)} (대조군 {len(ctrl)}개)",
                   f"< α_K={alpha_k:.4f}",
                   "대조군이 없으면 자동 실패다. random_ctrl 은 삭제할 수 없다.")
    )

    # 6. 폴드 일관성
    folds = [f for f in (fold_expectancies or []) if f == f]
    ratio = float(np.mean([f > 0 for f in folds])) if folds else np.nan
    rep.results.append(
        GateResult("6", "폴드 일관성", bool(folds and ratio >= float(c["fold_consistency_min"])),
                   f"{_fmt(ratio)} ({len(folds)}폴드)", f">= {c['fold_consistency_min']}")
    )

    # 7. 과적합 경고 (IS/OOS 샤프 비)
    if is_sharpe is not None and oos_sharpe is not None and np.isfinite(oos_sharpe) and abs(oos_sharpe) > 1e-9:
        ratio7 = float(is_sharpe / oos_sharpe)
        ok7 = ratio7 <= float(c["is_oos_sharpe_ratio_max"])
    else:
        ratio7, ok7 = np.nan, False
    rep.results.append(
        GateResult("7", "과적합 (IS/OOS 샤프비)", ok7, _fmt(ratio7), f"<= {c['is_oos_sharpe_ratio_max']}",
                   "IS/OOS 를 못 계산하면 통과시키지 않는다.")
    )

    # 8. 거래 수
    rep.results.append(
        GateResult("8", "거래 수", n_closed >= int(c["min_trades"]), n_closed, f">= {c['min_trades']}")
    )

    # §7.6 계열별 추가 게이트
    rep.results.extend(evaluate_family(family, metrics, family_context or {}))
    return rep
