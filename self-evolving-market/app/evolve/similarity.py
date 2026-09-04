"""§10.4 다양성·재제안 방지.

텍스트 + AST 구조 유사도 > 0.9 이면 반려. retired 재제안은 레짐이 다를 때만.
외부 임베딩 모델에 의존하지 않는다 — 결정론과 오프라인 실행을 위해 자체 계산한다.
"""

from __future__ import annotations

import ast
import re
from collections import Counter

TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣_]+")


def _tokens(text: str) -> Counter:
    return Counter(t.lower() for t in TOKEN_RE.findall(text or ""))


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    num = sum(a[t] * b[t] for t in common)
    da = sum(v * v for v in a.values()) ** 0.5
    db = sum(v * v for v in b.values()) ** 0.5
    return num / (da * db) if da and db else 0.0


def ast_profile(code: str) -> Counter:
    """AST 노드 타입 + 이름의 다중집합. 변수명만 바꾼 복제를 잡는다."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return Counter()
    prof: Counter = Counter()
    for node in ast.walk(tree):
        prof[type(node).__name__] += 1
        if isinstance(node, ast.Attribute):
            prof[f"attr:{node.attr}"] += 1
        elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            prof[f"const:{round(float(node.value), 4)}"] += 1
        elif isinstance(node, ast.Compare):
            prof["cmp:" + "".join(type(o).__name__ for o in node.ops)] += 1
    return prof


def similarity(text_a: str, code_a: str, text_b: str, code_b: str, *, text_weight: float = 0.4) -> float:
    """0~1. 텍스트 유사도와 AST 유사도의 가중합."""
    t = _cosine(_tokens(text_a), _tokens(text_b))
    c = _cosine(ast_profile(code_a), ast_profile(code_b))
    return float(text_weight * t + (1 - text_weight) * c)


def is_duplicate(
    candidate: tuple[str, str],
    existing: list[tuple[str, str]],
    threshold: float = 0.90,
) -> tuple[bool, float, int]:
    """(중복 여부, 최대 유사도, 가장 비슷한 항목의 인덱스)."""
    best, best_i = 0.0, -1
    for i, (text, code) in enumerate(existing):
        s = similarity(candidate[0], candidate[1], text, code)
        if s > best:
            best, best_i = s, i
    return best > threshold, best, best_i


def family_quota_ok(family: str, chosen: list[str], max_per_cycle: int = 2) -> tuple[bool, str]:
    """사이클당 같은 계열 <= 2 (§10.4)."""
    n = sum(1 for f in chosen if f == family)
    if n >= max_per_cycle:
        return False, f"이번 사이클에서 {family} 계열이 이미 {n}개입니다 (상한 {max_per_cycle})."
    return True, "계열 쿼터 여유"


def retired_reproposal_ok(retired_regime: str | None, current_regime: str) -> tuple[bool, str]:
    """retired 재제안은 레짐이 다를 때만 (§10.4)."""
    if retired_regime is None:
        return True, "은퇴 당시 레짐 기록 없음"
    if retired_regime == current_regime:
        return False, f"은퇴 당시와 같은 레짐('{current_regime}')에서는 재제안할 수 없습니다."
    return True, f"레짐이 '{retired_regime}' → '{current_regime}' 로 달라졌습니다."
