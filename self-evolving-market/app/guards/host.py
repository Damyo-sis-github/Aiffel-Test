"""#31 회사 장비에서 실행 금지."""

from __future__ import annotations

import os
import platform

from app.config import allowed_hosts
from app.guards.base import GuardResult


def current_hostname() -> str:
    return (platform.node() or os.environ.get("COMPUTERNAME") or "").strip()


def domain_suffix() -> str:
    """도메인 조인 시 나타나는 DNS 도메인. 조인되지 않았으면 빈 문자열.

    `USERDOMAIN_ROAMINGPROFILE` 은 **도메인에 조인되지 않은 PC 에서도** 항상 설정되고,
    그 값은 컴퓨터 이름이다. 그걸 그대로 도메인으로 읽으면 개인 노트북이 전부
    "도메인 조인됨"으로 판정되어 daily 가 영원히 실행되지 않는다.
    실제 기기(DESKTOP-RQQ0696)에서 이 오탐이 확인되었다 — 합성 환경에는 이 환경변수가
    없어서 테스트가 전부 통과하고 있었다.

    그래서 컴퓨터 이름과 같은 값은 도메인으로 치지 않는다.
    """
    dns = (os.environ.get("USERDNSDOMAIN") or "").strip()
    if dns:
        return dns                      # 이 변수는 도메인 조인 시에만 존재한다
    roaming = (os.environ.get("USERDOMAIN_ROAMINGPROFILE") or "").strip()
    host = current_hostname()
    if roaming and roaming.lower() == host.lower():
        return ""                       # 로컬 계정 — 조인 아님
    return roaming


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
        return GuardResult("host", False, f"도메인 조인된 기기입니다 (도메인={dom}).", details)

    if not any(low == a.lower() for a in allowed):
        return GuardResult("host", False, f"등록되지 않은 기기입니다: {host}", details)

    return GuardResult("host", True, f"등록된 기기: {host}", details)
