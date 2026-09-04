"""§7.5 / §7.6 채택 게이트. **결정론 코드. LLM 은 이 디렉터리를 수정할 수 없다** (§10.1 G2).

이 패키지의 해시는 config/protected.lock 에 봉인되고, daily 시작 시 검사된다 (#6).
"""

from app.evaluator.gates import GateReport, GateResult, evaluate
from app.evaluator.stats import (
    alpha_for_k,
    bootstrap_p_value,
    cluster_bootstrap_ci,
    wilson_ci,
)

__all__ = [
    "GateReport",
    "GateResult",
    "alpha_for_k",
    "bootstrap_p_value",
    "cluster_bootstrap_ci",
    "evaluate",
    "wilson_ci",
]
