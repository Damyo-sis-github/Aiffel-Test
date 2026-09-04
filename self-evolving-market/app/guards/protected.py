"""#6 평가기 오염 — protected.lock 해시 검사."""

from __future__ import annotations

from app.config import check_protected_lock
from app.guards.base import GuardResult


def protected_lock_guard() -> GuardResult:
    st = check_protected_lock()
    return GuardResult(
        "protected_lock",
        st.ok,
        st.message,
        {"expected": st.expected, "actual": st.actual, "targets": list(st.targets)},
    )
