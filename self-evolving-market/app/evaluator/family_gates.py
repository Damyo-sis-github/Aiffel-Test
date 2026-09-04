"""§7.6 도구 계열별 추가 게이트·제약.

하드 제약(보유 5일, 손절 -8%, 레짐 진입 제한)은 **엔진**이 강제한다.
여기서는 그 제약이 지켜진 결과가 계열 기준을 만족하는지만 본다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from app.config import gates as load_gates

if TYPE_CHECKING:  # 순환 import 회피
    from app.evaluator.gates import GateResult


def _result(number: str, name: str, passed: bool, observed: Any, threshold: Any, detail: str = "") -> GateResult:
    from app.evaluator.gates import GateResult as GR

    return GR(number, name, passed, observed, threshold, detail)


def evaluate_family(family: str, metrics: dict, context: dict) -> list[GateResult]:
    cfg = (load_gates().get("families") or {}).get(family) or {}
    if not cfg:
        return []

    out: list[GateResult] = []
    mdd = float(metrics.get("mdd", np.nan))
    if (thr := cfg.get("mdd_max")) is not None:
        out.append(
            _result("F1", f"{family} MDD", bool(np.isfinite(mdd) and mdd <= float(thr)),
                    f"{mdd:.4f}" if np.isfinite(mdd) else "n/a", f"<= {thr}")
        )

    if (thr := cfg.get("worst_single_trade_loss_max")) is not None:
        worst = float(metrics.get("worst_trade", np.nan))
        loss = -worst if np.isfinite(worst) else np.nan
        out.append(
            _result("F2", f"{family} 최악 단일 거래 손실",
                    bool(np.isfinite(loss) and loss <= float(thr)),
                    f"{loss:.4f}" if np.isfinite(loss) else "n/a", f"<= {thr}",
                    "숏스퀴즈 노출을 잡는 게이트다.")
        )

    if (thr := cfg.get("sideways_regime_expectancy_min")) is not None:
        e = context.get("sideways_expectancy", np.nan)
        e = float(e) if e == e else np.nan
        out.append(
            _result("F3", f"{family} 횡보 레짐 기대값",
                    bool(np.isfinite(e) and e > float(thr)),
                    f"{e:.4f}" if np.isfinite(e) else "n/a", f"> {thr}",
                    "레버리지·인버스의 변동성 감쇠가 횡보장에서 얼마나 녹는지를 본다.")
        )
    return out


def family_activation_open(family: str, active_by_family: dict[str, int]) -> tuple[bool, str]:
    """§7.6 / #28 계열 활성화 순서.

    LONG 또는 ETF_ROT 에 active 전략이 1개 이상 있어야 SHORT_US/LEV_ETF/INV_ETF 가 열린다.
    """
    cfg = load_gates().get("family_activation_order") or {}
    gated = set(cfg.get("gated") or ())
    if family not in gated:
        return True, "게이트 대상 계열이 아님"
    prereq = list(cfg.get("gating_prerequisite") or ())
    total = sum(int(active_by_family.get(f, 0)) for f in prereq)
    if total >= 1:
        return True, f"선행 계열 active {total}개 ({', '.join(prereq)})"
    return False, (
        f"{family} 는 아직 열리지 않았습니다. {' 또는 '.join(prereq)} 에 active 전략이 "
        "1개 이상 있어야 합니다 (§7.6 계열 활성화 순서)."
    )
