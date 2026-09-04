"""#31 회사 장비에서 실행 금지."""

from __future__ import annotations

import os
import platform

from app.config import allowed_hosts
from app.guards.base import GuardResult


def current_hostname() -> str:
    return (platform.node() or os.environ.get("COMPUTERNAME") or "").strip()


def domain_suffix() -> str:
    """Windows 도메인 조인 여부 힌트."""
    return (os.environ.get("USERDNSDOMAIN") or os.environ.get("USERDOMAIN_ROAMINGPROFILE") or "").strip()


def host_guard() -> GuardResult:
    cfg = allowed_hosts()
    host = current_hostname()
    allowed = [str(h) for h in (cfg.get("hostnames") or [])]
    patterns = [str(p).lower() for p in (cfg.get("corporate_patterns") or [])]
    details = {"hostname": host, "allowed": allowed, "domain": domain_suffix()}

    if not host:
        return GuardResult("host", False, "hostname 을 읽을 수 없습니다.", details)

    # fail-closed: 화이트리스트가 비어 있으면 실행하지 않는다.
    if not allowed:
        return GuardResult(
            "host",
            False,
            f"allowed_hosts.yaml 의 hostnames 가 비어 있습니다. 이 기기('{host}')를 등록하십시오. "
            "(§17 결정 대기: 노트북 hostname)",
            details,
        )

    low = host.lower()
    if hit := next((p for p in patterns if p and p in low), None):
        return GuardResult("host", False, f"회사 장비 패턴 '{hit}' 이 hostname 에 있습니다: {host}", details)

    dom = domain_suffix().lower()
    if cfg.get("reject_if_domain_joined", True) and dom:
        return GuardResult("host", False, f"도메인 조인된 기기입니다 (USERDNSDOMAIN={dom}).", details)

    if not any(low == a.lower() for a in allowed):
        return GuardResult("host", False, f"등록되지 않은 기기입니다: {host}", details)

    return GuardResult("host", True, f"등록된 기기: {host}", details)
