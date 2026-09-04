from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

BYPASS_ENV = "QUANT_GUARD_BYPASS"


@dataclass(frozen=True)
class GuardResult:
    name: str
    ok: bool
    reason: str = ""
    details: dict = field(default_factory=dict)
    bypassed: bool = False

    def __str__(self) -> str:
        mark = "OK" if self.ok else "거부"
        suffix = " (BYPASS)" if self.bypassed else ""
        return f"[{self.name}] {mark}{suffix}" + (f" — {self.reason}" if self.reason else "")


class GuardViolation(RuntimeError):
    """가드 위반. 실행하지 않고 텔레그램으로 사유를 보낸다."""

    def __init__(self, results: list[GuardResult]):
        self.results = results
        failed = [r for r in results if not r.ok]
        super().__init__("실행 조건 미충족:\n" + "\n".join(f"  - {r}" for r in failed))

    @property
    def failed(self) -> list[GuardResult]:
        return [r for r in self.results if not r.ok]


def bypass_enabled() -> bool:
    """개발/CI 전용. 켜지면 모든 리포트에 배너가 박힌다."""
    return os.environ.get(BYPASS_ENV) == "1"


def run_guards(
    guards: Iterable[Callable[[], GuardResult]],
    *,
    raise_on_fail: bool = True,
) -> list[GuardResult]:
    results: list[GuardResult] = []
    bypass = bypass_enabled()
    for g in guards:
        try:
            r = g()
        except Exception as exc:  # 가드 자체가 터지면 fail-closed
            r = GuardResult(name=getattr(g, "__name__", "unknown"), ok=False, reason=f"가드 실행 실패: {exc}")
        if not r.ok and bypass:
            r = GuardResult(name=r.name, ok=True, reason=r.reason, details=r.details, bypassed=True)
        results.append(r)
    if raise_on_fail and any(not r.ok for r in results):
        raise GuardViolation(results)
    return results
