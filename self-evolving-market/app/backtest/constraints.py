"""하드 제약을 읽는 **유일한** 곳 (§7.3, §7.6, CLAUDE.md 8).

왜 따로 뺐나
  손절폭·최대보유일·호라이즌 상한을 해석하는 코드가 `backtest/engine.py` 와
  `pipeline/positions.py` 에 글자 그대로 복제되어 있었다. 백테스트가 게이트를
  통과시킨 규칙과 페이퍼 계좌가 실제로 도는 규칙이 **서로 다른 코드**였다는 뜻이다.
  한쪽만 고치면 아무 경고 없이 갈라지고, 그러면 게이트 판정은 실제로 돌지 않는
  전략에 대한 판정이 된다. 갈라짐을 테스트로 잡을 수도 없다 — 두 구현이 각자
  자기 코드에 대해 옳기 때문이다.

여기 있는 것은 전부 순수 함수다. 상태도, I/O 도 없다 (설정 읽기 제외).
전략은 이 값을 정하지 않는다. 엔진이 `config/risk.yaml` 에서 읽는다.
"""

from __future__ import annotations

from typing import Any

from app.config import risk as load_risk

DEFAULT_STOP_PCT = 0.15


def _risk(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    return cfg if cfg is not None else load_risk()


def stop_pct(family: str, horizon: int, cfg: dict[str, Any] | None = None) -> float:
    """계열·호라이즌별 손절폭. #20 SHORT 의 -8% 는 여기서 나오고 우회 경로가 없다."""
    stops = _risk(cfg)["stop_loss"].get(family, {})
    if "default" in stops:
        return float(stops["default"])
    key = f"h{horizon}"
    if key in stops:
        return float(stops[key])
    # 호라이즌이 표에 없으면 가장 가까운 **큰** 호라이즌의 값을 쓴다.
    # 작은 쪽으로 내림하면 손절이 느슨해진다 — 안전한 방향으로만 근사한다.
    keys = sorted(((int(k[1:]), v) for k, v in stops.items() if k.startswith("h")), key=lambda kv: kv[0])
    for h, v in keys:
        if horizon <= h:
            return float(v)
    return float(keys[-1][1]) if keys else DEFAULT_STOP_PCT


def max_hold_days(family: str, cfg: dict[str, Any] | None = None) -> int | None:
    """#19 LEV/INV 5 거래일. None 은 '제한 없음'."""
    v = (_risk(cfg)["hard_constraints"].get("max_hold_days") or {}).get(family)
    return int(v) if v is not None else None


def enforce_horizon(family: str, horizon: int, cfg: dict[str, Any] | None = None) -> int:
    """#19 LEV/INV 는 호라이즌 자체를 5일로 깎는다. 전략이 60일을 원해도 5일이다."""
    cap = (_risk(cfg)["hard_constraints"].get("max_horizon_days") or {}).get(family)
    return min(horizon, int(cap)) if cap is not None else horizon


def allowed_regimes(family: str, cfg: dict[str, Any] | None = None) -> tuple[str, ...] | None:
    v = (_risk(cfg)["hard_constraints"].get("regime_required") or {}).get(family)
    return tuple(v) if v else None
