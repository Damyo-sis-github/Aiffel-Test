"""하드 제약은 한 곳에서만 해석된다 (CLAUDE.md 8).

손절폭·최대보유일·호라이즌 상한을 읽는 코드가 `backtest/engine.py` 와
`pipeline/positions.py` 에 글자 그대로 복제되어 있었다. 백테스트가 게이트를
통과시킨 규칙과 페이퍼 계좌가 실제로 도는 규칙이 서로 다른 코드였다.

이 갈라짐은 값 비교로는 못 잡는다 — 복제된 두 구현은 갈라지기 전까지 같은 값을
낸다. 그래서 여기서는 **같은 함수를 가리키는지**를 본다.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.backtest import constraints
from app.backtest.costs import CostModel
from app.backtest.engine import BacktestEngine
from app.pipeline import positions

SRC = Path(__file__).resolve().parent.parent / "app"


def test_paper_path_uses_the_same_functions(sandbox):
    """이름만 다른 통로여야 한다. 별도 구현이면 이 동일성이 깨진다."""
    assert positions.stop_pct_for is constraints.stop_pct
    assert positions.max_hold_for is constraints.max_hold_days
    assert positions.enforce_horizon is constraints.enforce_horizon


def test_engine_delegates_rather_than_reimplements(sandbox):
    """엔진 메서드 본문이 constraints 를 부르는 한 줄이어야 한다."""
    for name in ("stop_pct", "max_hold_days", "enforce_horizon", "allowed_regimes"):
        body = inspect.getsource(getattr(BacktestEngine, name))
        assert "constraints." in body, f"{name} 이 제약을 다시 구현하고 있습니다"


def test_engine_and_paper_agree_on_every_family(sandbox):
    """두 경로가 실제로 같은 숫자를 낸다 — 위임이 끊기면 여기서도 깨진다.

    엔진은 제약 조회에 패널·가격·비용을 쓰지 않으므로 빈 껍데기로 충분하다.
    """
    from app.config import risk as load_risk

    cfg = load_risk()
    eng = BacktestEngine(panel=None, book=None, cost_model=CostModel.from_config())
    families = sorted(set(cfg["stop_loss"]) | set(cfg["hard_constraints"].get("max_hold_days") or {}))
    assert families, "risk.yaml 에 계열이 하나도 없습니다"
    for fam in families:
        for h in (1, 3, 5, 10, 20, 60, 120):
            assert eng.stop_pct(fam, h) == positions.stop_pct_for(fam, h, cfg)
            assert eng.enforce_horizon(fam, h) == positions.enforce_horizon(fam, h, cfg)
        assert eng.max_hold_days(fam) == positions.max_hold_for(fam, cfg)


def test_short_stop_is_8_percent_and_lev_hold_is_5_days(sandbox):
    """#19 #20 은 숫자로 못 박는다. 설정이 조용히 느슨해지면 여기서 걸린다."""
    assert constraints.stop_pct("SHORT_US", 20) == 0.08
    assert constraints.max_hold_days("LEV_ETF") == 5
    assert constraints.max_hold_days("INV_ETF") == 5
    assert constraints.enforce_horizon("LEV_ETF", 60) == 5


def test_stop_falls_back_to_the_tighter_side(sandbox):
    """표에 없는 호라이즌은 **더 큰** 호라이즌 값으로 올린다.

    작은 쪽으로 내리면 손절이 느슨해진다. 근사는 안전한 방향으로만 한다.
    """
    cfg = {"stop_loss": {"X": {"h10": 0.05, "h60": 0.12}}, "hard_constraints": {}}
    assert constraints.stop_pct("X", 1, cfg) == 0.05
    assert constraints.stop_pct("X", 30, cfg) == 0.12
    assert constraints.stop_pct("X", 999, cfg) == 0.12      # 표 밖은 마지막 값


def test_no_other_module_reads_the_constraint_config(sandbox):
    """설정에서 제약을 **꺼내는** 코드는 constraints.py 뿐이어야 한다.

    같은 문자열이 청산 사유 라벨(`broker.close(..., "stop_loss")`)이나 리포트의
    딕셔너리 키로도 쓰인다. 그건 해석이 아니므로 잡지 않는다.
    구분 기준은 **설정을 첨자로 읽는가**(`cfg["stop_loss"]`) 하나다.
    """
    keys = {"stop_loss", "max_hold_days", "max_horizon_days"}
    offenders = []
    for f in sorted(SRC.rglob("*.py")):
        if f.name == "constraints.py" or "__pycache__" in f.parts:
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"), str(f))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.slice, ast.Constant)
                    and node.slice.value in keys):
                offenders.append(f"{f.relative_to(SRC)}:{node.lineno} [{node.slice.value!r}]")
    assert not offenders, (
        "하드 제약을 constraints.py 밖에서 설정에서 직접 꺼내고 있습니다. "
        "해석이 두 벌이 되면 백테스트와 페이퍼가 조용히 갈라집니다:\n  "
        + "\n  ".join(offenders)
    )
