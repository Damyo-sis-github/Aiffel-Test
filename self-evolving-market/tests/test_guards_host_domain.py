"""#31 호스트 가드의 도메인 조인 판정.

`USERDOMAIN_ROAMINGPROFILE` 은 도메인에 조인되지 않은 PC 에서도 항상 설정되고
그 값은 컴퓨터 이름이다. 그걸 도메인으로 읽으면 **개인 노트북이 전부 회사 장비로
판정되어** daily 가 영원히 실행되지 않는다. 실기기(DESKTOP-RQQ0696)에서 확인된
오탐이며, 합성 환경에는 그 환경변수가 없어 테스트가 전부 통과하고 있었다.
"""

from __future__ import annotations

import pytest

from app.guards.host import current_hostname, domain_suffix, host_guard


@pytest.fixture()
def as_host(monkeypatch):
    def _set(name: str, **env):
        monkeypatch.setattr("app.guards.host.platform.node", lambda: name)
        for k in ("USERDNSDOMAIN", "USERDOMAIN_ROAMINGPROFILE", "COMPUTERNAME"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    return _set


def test_roaming_profile_equal_to_hostname_is_not_a_domain(as_host):
    """로컬 계정 PC. 이게 오탐의 원인이었다."""
    as_host("DESKTOP-RQQ0696", USERDOMAIN_ROAMINGPROFILE="DESKTOP-RQQ0696")
    assert domain_suffix() == ""


def test_case_differences_still_count_as_not_joined(as_host):
    as_host("DESKTOP-RQQ0696", USERDOMAIN_ROAMINGPROFILE="desktop-rqq0696")
    assert domain_suffix() == ""


def test_real_dns_domain_is_reported(as_host):
    """USERDNSDOMAIN 은 도메인 조인 시에만 존재한다. 있으면 그대로 도메인이다."""
    as_host("SOME-PC", USERDNSDOMAIN="corp.example.com",
            USERDOMAIN_ROAMINGPROFILE="SOME-PC")
    assert domain_suffix() == "corp.example.com"


def test_roaming_profile_different_from_hostname_is_a_domain(as_host):
    """조인된 PC 에서는 이 값이 NetBIOS 도메인명이라 컴퓨터 이름과 다르다."""
    as_host("SOME-PC", USERDOMAIN_ROAMINGPROFILE="DRBDONGIL")
    assert domain_suffix() == "DRBDONGIL"


def test_local_account_machine_passes_the_guard_when_registered(sandbox, as_host):
    """등록된 개인 노트북은 통과해야 한다 — 오탐이면 여기서 막힌다."""
    from app.config import _load_yaml_cached
    from app.paths import config_dir

    as_host("DESKTOP-RQQ0696", USERDOMAIN_ROAMINGPROFILE="DESKTOP-RQQ0696")
    p = config_dir() / "allowed_hosts.yaml"
    p.write_text("hostnames: [DESKTOP-RQQ0696]\ncorporate_patterns: []\n"
                 "reject_if_domain_joined: true\n", encoding="utf-8")
    _load_yaml_cached.cache_clear()
    r = host_guard()
    assert r.ok, r.reason


def test_domain_joined_machine_is_still_rejected(sandbox, as_host):
    """오탐을 고치면서 진짜 회사 PC 를 통과시키면 안 된다."""
    from app.config import _load_yaml_cached
    from app.paths import config_dir

    as_host("SOME-PC", USERDNSDOMAIN="corp.example.com")
    p = config_dir() / "allowed_hosts.yaml"
    p.write_text("hostnames: [SOME-PC]\ncorporate_patterns: []\n"
                 "reject_if_domain_joined: true\n", encoding="utf-8")
    _load_yaml_cached.cache_clear()
    r = host_guard()
    assert not r.ok and "도메인 조인" in r.reason


def test_hostname_still_readable(as_host):
    as_host("DESKTOP-RQQ0696", USERDOMAIN_ROAMINGPROFILE="DESKTOP-RQQ0696")
    assert current_hostname() == "DESKTOP-RQQ0696"
