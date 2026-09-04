"""소스 코드 자체를 검사하는 정책 테스트.

#16 실계좌 주문 코드·엔드포인트가 존재하면 CI 실패
#17 시크릿 하드코딩 금지
PIT 우회 읽기 금지 — read_unfiltered 는 무결성 검사 밖에서 호출될 수 없다
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "app"


def _py_files(root: Path) -> list[Path]:
    return [p for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts]


# ---------------------------------------------------------------- #16 실계좌 주문
# 주문 전송을 뜻하는 경로·심볼. 문자열이 아니라 패턴으로 잡는다.
FORBIDDEN_PATTERNS = [
    r"/v\d+/orders?\b",
    r"place[_-]?order",
    r"submit[_-]?order\b",
    r"cancel[_-]?order",
    r"modify[_-]?order",
    r"class\s+LiveBroker",
    r"openapi\.koreainvestment\.com",
    r"api\.tossinvest\.com/.*order",
]
# 이 파일 자신은 패턴 목록을 갖고 있으므로 제외한다.
POLICY_EXEMPT = {"test_source_policy.py"}


def test_no_live_order_code_anywhere():
    hits = []
    for f in _py_files(APP) + _py_files(REPO / "tests"):
        if f.name in POLICY_EXEMPT:
            continue
        text = f.read_text(encoding="utf-8")
        for pat in FORBIDDEN_PATTERNS:
            for m in re.finditer(pat, text, re.IGNORECASE):
                line = text[: m.start()].count("\n") + 1
                hits.append(f"{f.relative_to(REPO)}:{line} → {m.group(0)}")
    assert not hits, "실계좌 주문 계열 코드가 발견되었습니다 (#16):\n" + "\n".join(hits)


def test_toss_adapter_is_the_only_toss_caller():
    callers = []
    for f in _py_files(APP):
        if f.name == "toss.py":
            continue
        if "tossinvest" in f.read_text(encoding="utf-8"):
            callers.append(str(f.relative_to(REPO)))
    assert not callers, f"토스 API 는 data/adapters/toss.py 한 곳에서만 호출해야 합니다: {callers}"


def test_toss_endpoint_allowlist_enforced(sandbox):
    from app.data.adapters.toss import TossEndpointRejected, assert_endpoint_allowed

    assert assert_endpoint_allowed("/v1/market/quote") == "/v1/market/quote"
    for bad in ("/v1/orders", "/v1/accounts/cash", "/v1/market/quote/../orders"):
        try:
            assert_endpoint_allowed(bad)
        except TossEndpointRejected:
            continue
        raise AssertionError(f"허용되지 않아야 할 엔드포인트가 통과했습니다: {bad}")


def test_toss_disabled_by_default(sandbox):
    from app.data.adapters.toss import TossDisabled, get

    try:
        get("/v1/market/quote")
    except TossDisabled:
        return
    raise AssertionError("토스 API 가 기본적으로 비활성이어야 합니다 (§17 발급 대기)")


def test_only_paper_broker_exists():
    from app.execution import broker as broker_mod

    src = Path(broker_mod.__file__).read_text(encoding="utf-8")
    assert "LiveBroker" not in src.replace("`LiveBroker`", "")  # 주석 속 언급은 허용
    impls = [p.name for p in _py_files(APP / "execution")]
    assert "paper_sim.py" in impls
    assert not any("live" in n for n in impls)


# ---------------------------------------------------------------- #17 시크릿
SECRET_PATTERNS = [
    r"(?i)(api[_-]?key|secret|token|password)\s*=\s*[\"'][A-Za-z0-9_\-]{16,}[\"']",
    r"bot\d{6,}:[A-Za-z0-9_\-]{30,}",          # 텔레그램 봇 토큰
    r"sk-[A-Za-z0-9]{20,}",
]


def test_no_hardcoded_secrets():
    hits = []
    for f in _py_files(APP) + _py_files(REPO / "tests"):
        if f.name in POLICY_EXEMPT:
            continue
        text = f.read_text(encoding="utf-8")
        for pat in SECRET_PATTERNS:
            for m in re.finditer(pat, text):
                hits.append(f"{f.relative_to(REPO)}: {m.group(0)[:40]}")
    assert not hits, "하드코딩된 시크릿 의심 (#17):\n" + "\n".join(hits)


def test_config_yaml_has_no_secrets():
    hits = []
    for f in sorted((REPO / "config").glob("*.yaml")):
        text = f.read_text(encoding="utf-8")
        for pat in SECRET_PATTERNS:
            if re.search(pat, text):
                hits.append(str(f.name))
    assert not hits, f"config 에 시크릿이 있습니다: {hits}"


def test_credentials_come_from_env_only():
    """자격증명은 os.environ 에서만 읽어야 한다."""
    src = (APP / "data" / "adapters" / "toss.py").read_text(encoding="utf-8")
    assert "os.environ.get(\"TOSS_API_KEY\")" in src
    assert "TOSS_API_KEY:" not in (REPO / "config" / "runtime.yaml").read_text(encoding="utf-8")


# ---------------------------------------------------------------- PIT 우회 금지
PIT_BYPASS_ALLOWED = {
    "app/data/pit_store.py",          # 정의처
    "app/data/integrity.py",          # 무결성 검사
}


def test_read_parquet_only_in_pit_store():
    hits = []
    for f in _py_files(APP):
        rel = f.relative_to(REPO).as_posix()
        if rel in PIT_BYPASS_ALLOWED:
            continue
        if "read_parquet" in f.read_text(encoding="utf-8"):
            hits.append(rel)
    assert not hits, f"PIT 저장소를 우회한 parquet 직접 읽기: {hits}"


def test_read_unfiltered_not_used_in_pipeline():
    """백테스트·피처·전략·포트폴리오는 PIT 우회 읽기를 쓸 수 없다."""
    hits = []
    for sub in ("backtest", "features", "strategies", "portfolio", "predictions", "evolve", "pipeline"):
        for f in _py_files(APP / sub):
            if "read_unfiltered" in f.read_text(encoding="utf-8"):
                hits.append(f.relative_to(REPO).as_posix())
    assert not hits, f"PIT 우회 읽기가 파이프라인에서 사용됨: {hits}"


def test_price_query_signature_requires_as_of():
    """as_of 기본값이 있는 가격 조회 함수는 존재해선 안 된다."""
    import inspect

    from app.data.pit_store import PITStore

    for name in ("prices", "macro", "fx", "universe"):
        sig = inspect.signature(getattr(PITStore, name))
        assert sig.parameters["as_of"].default is inspect.Parameter.empty, f"{name} 의 as_of 에 기본값이 있습니다"


# ---------------------------------------------------------------- #27 리스크 우회 금지
def test_strategies_do_not_set_risk_parameters():
    """전략 코드가 손절·익스포저 파라미터를 정하면 안 된다 (엔진 하드 제약)."""
    banned = re.compile(r"(stop_loss|stop_pct|gross_notional|short_notional|killswitch|max_hold_days)\s*=")
    hits = []
    for f in _py_files(APP / "strategies"):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if banned.search(line):
                hits.append(f"{f.relative_to(REPO)}:{i}: {line.strip()}")
    assert not hits, "전략이 리스크 파라미터를 건드립니다 (#27):\n" + "\n".join(hits)


def test_random_control_exists():
    """§7.2 random_ctrl 은 삭제 금지."""
    from app.strategies.registry import CONTROL_ID, seed_strategies

    assert any(s.id == CONTROL_ID for s in seed_strategies()), "random_ctrl 이 사라졌습니다 (삭제 금지)"


def test_all_seed_families_present():
    from app.strategies.base import FAMILIES
    from app.strategies.registry import seed_strategies

    families = {str(s.family) for s in seed_strategies()}
    assert set(FAMILIES) <= families, f"누락된 계열: {set(FAMILIES) - families}"
