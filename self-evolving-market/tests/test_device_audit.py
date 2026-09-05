"""§11.8 기기 가시성 감사 + 승격 감시.

Windows 전용 수집은 이 환경에서 돌릴 수 없으므로, **파싱과 판정을 순수 함수로 분리**해
실제 `dsregcmd /status` 출력 형식으로 검증한다. 수집부(subprocess)만 미검증으로 남는다.
"""

from __future__ import annotations

import pytest

from app.guards.device import evaluate
from app.guards.device_audit import (
    DeviceAudit,
    Finding,
    Level,
    detect_agents,
    parse_dsregcmd,
)

# 사용자 실제 환경과 같은 형태: WorkplaceJoined=YES, MdmUrl 없음
DSREG_REGISTERED_NO_MDM = """
+----------------------------------------------------------------------+
| Device State                                                         |
+----------------------------------------------------------------------+

             AzureAdJoined : NO
          EnterpriseJoined : NO
              DomainJoined : NO
           WorkplaceJoined : YES
         DeviceAuthStatus : SUCCESS

+----------------------------------------------------------------------+
| User State                                                           |
+----------------------------------------------------------------------+

                NgcSet : NO
       WorkplaceJoined : YES
"""

DSREG_MDM_ENROLLED = """
+----------------------------------------------------------------------+
| Device State                                                         |
+----------------------------------------------------------------------+

             AzureAdJoined : YES
              DomainJoined : NO
           WorkplaceJoined : NO

+----------------------------------------------------------------------+
| Tenant Details                                                       |
+----------------------------------------------------------------------+

                TenantName : Contoso
                    MdmUrl : https://enrollment.manage.microsoft.com/enrollmentserver/discovery.svc
"""

DSREG_CLEAN = """
+----------------------------------------------------------------------+
| Device State                                                         |
+----------------------------------------------------------------------+

             AzureAdJoined : NO
              DomainJoined : NO
           WorkplaceJoined : NO
                    MdmUrl :
"""


# ---------------------------------------------------------------- 파싱
def test_parse_detects_workplace_joined():
    d = parse_dsregcmd(DSREG_REGISTERED_NO_MDM)
    assert d["WorkplaceJoined"] == ["YES", "YES"]     # 두 섹션에 등장
    assert d["AzureAdJoined"] == ["NO"]
    assert "MdmUrl" not in d


def test_parse_detects_mdm_url():
    d = parse_dsregcmd(DSREG_MDM_ENROLLED)
    assert d["MdmUrl"][0].startswith("https://enrollment.manage.microsoft.com")


def test_parse_treats_empty_mdm_url_as_absent_value():
    d = parse_dsregcmd(DSREG_CLEAN)
    assert d["MdmUrl"] == [""]           # 키는 있지만 값이 비어 있다
    assert all(v.strip() == "" for v in d["MdmUrl"])


def test_parse_ignores_table_borders():
    d = parse_dsregcmd(DSREG_CLEAN)
    assert not any(k.startswith("+") or k.startswith("|") for k in d)


# ---------------------------------------------------------------- 에이전트 탐지
@pytest.mark.parametrize(
    "display",
    [
        "CrowdStrike Falcon Sensor",
        "SentinelOne Agent",
        "Fasoo Enterprise DRM",
        "소프트캠프 Document Security",
        "AhnLab V3 Endpoint Security",
        "MarkAny Document SAFER",
        "Cortex XDR",
    ],
)
def test_detect_agents_finds_edr_and_dlp(display):
    inv = {"services": [{"Name": "svc", "DisplayName": display, "Status": "Running"}], "apps": []}
    assert detect_agents(inv), f"에이전트를 못 잡았습니다: {display}"


def test_detect_agents_finds_defender_for_endpoint():
    inv = {"services": [{"Name": "Sense", "DisplayName": "Windows Defender Advanced Threat Protection",
                         "Status": "Running"}], "apps": []}
    hits = detect_agents(inv)
    assert any("defender for endpoint" in h[0] for h in hits)


def test_plain_defender_is_not_flagged_as_edr():
    """WinDefend 단독은 일반 백신이다. 이걸로 거부하면 모든 Windows 가 막힌다."""
    inv = {"services": [{"Name": "WinDefend", "DisplayName": "Microsoft Defender Antivirus Service",
                         "Status": "Running"}], "apps": []}
    assert not detect_agents(inv)


def test_detect_agents_clean_machine():
    inv = {"services": [{"Name": "Spooler", "DisplayName": "Print Spooler", "Status": "Running"}],
           "apps": [{"DisplayName": "7-Zip"}, {"DisplayName": "Python 3.11"}]}
    assert detect_agents(inv) == []


# ---------------------------------------------------------------- 레벨 판정
def _audit(*findings: Finding) -> DeviceAudit:
    return DeviceAudit(platform="Windows", findings=list(findings))


def _registered() -> DeviceAudit:
    return _audit(Finding(Level.REGISTERED, "WorkplaceJoined", "등록됨"))


def _mdm() -> DeviceAudit:
    return _audit(Finding(Level.REGISTERED, "WorkplaceJoined", "등록됨"),
                  Finding(Level.MDM, "MdmUrl", "https://..."))


BASE_CFG = {
    "max_level": 1,
    "overrides": {"allow_registered_without_mdm": False},
    "never_allow": ["mdm", "agent"],
    "watch_promotion": True,
    "skip_on_non_windows": True,
}


def test_level_is_max_of_findings():
    a = _audit(Finding(Level.SYNC, "OneDrive", "x"), Finding(Level.REGISTERED, "WorkplaceJoined", "y"))
    assert a.level is Level.REGISTERED


def test_registered_rejected_by_default():
    r = evaluate(_registered(), BASE_CFG, None)
    assert not r.ok and "상한" in r.reason


def test_registered_allowed_with_explicit_override():
    cfg = {**BASE_CFG, "overrides": {"allow_registered_without_mdm": True,
                                     "acknowledged_by": "본인", "acknowledged_at": "2026-09-05"}}
    r = evaluate(_registered(), cfg, None)
    assert r.ok and "정책 예외" in r.reason


def test_mdm_rejected_even_with_override():
    """예외 승인으로도 MDM 은 넘을 수 없다."""
    cfg = {**BASE_CFG, "max_level": 4,
           "overrides": {"allow_registered_without_mdm": True}}
    r = evaluate(_mdm(), cfg, None)
    assert not r.ok and "MDM" in r.reason


def test_agent_rejected_even_with_override():
    cfg = {**BASE_CFG, "max_level": 4, "overrides": {"allow_registered_without_mdm": True}}
    a = _audit(Finding(Level.AGENT, "보안 에이전트", "crowdstrike 감지 — 서비스 'Falcon'"))
    r = evaluate(a, cfg, None)
    assert not r.ok and "에이전트" in r.reason


def test_sync_only_passes_at_default_policy():
    a = _audit(Finding(Level.SYNC, "OneDrive 동기화 루트", r"C:\Users\u\OneDrive - 회사"))
    assert evaluate(a, BASE_CFG, None).ok


# ---------------------------------------------------------------- 승격 감시
def test_promotion_from_registered_to_mdm_halts():
    """이 가드의 존재 이유. 레벨 2 → 3 승격을 잡는다."""
    cfg = {**BASE_CFG, "overrides": {"allow_registered_without_mdm": True}}
    last = {"level": int(Level.REGISTERED)}
    r = evaluate(_mdm(), cfg, last)
    assert not r.ok
    assert "올라갔습니다" in r.reason or "MDM" in r.reason


def test_promotion_detected_even_when_level_still_allowed():
    """상한 안이어도 레벨이 올라갔으면 멈춘다 — 정책이 바뀐 신호이기 때문."""
    cfg = {**BASE_CFG, "max_level": 4, "overrides": {"allow_registered_without_mdm": True}}
    last = {"level": int(Level.SYNC)}
    r = evaluate(_registered(), cfg, last)
    assert not r.ok and "올라갔습니다" in r.reason
    assert r.details["previous_level"] == int(Level.SYNC)


def test_no_promotion_when_level_unchanged():
    cfg = {**BASE_CFG, "overrides": {"allow_registered_without_mdm": True}}
    last = {"level": int(Level.REGISTERED)}
    assert evaluate(_registered(), cfg, last).ok


def test_level_going_down_is_fine():
    cfg = {**BASE_CFG, "overrides": {"allow_registered_without_mdm": True}}
    last = {"level": int(Level.MDM)}
    assert evaluate(_registered(), cfg, last).ok


# ---------------------------------------------------------------- 플랫폼
def test_non_windows_is_skipped_by_default():
    a = DeviceAudit(platform="Linux", checked=False)
    assert evaluate(a, BASE_CFG, None).ok


def test_non_windows_can_be_made_fail_closed():
    a = DeviceAudit(platform="Linux", checked=False)
    r = evaluate(a, {**BASE_CFG, "skip_on_non_windows": False}, None)
    assert not r.ok


# ---------------------------------------------------------------- 통합
def test_device_guard_is_in_daily_chain():
    from app.guards import DAILY_GUARDS, device_guard

    assert device_guard in DAILY_GUARDS


def test_shipped_policy_is_conservative(sandbox):
    """저장소에 커밋되는 기본 정책은 명세 §11.8 전제(레벨 ≤ 1)를 지켜야 한다."""
    from app.guards.device import policy

    cfg = policy()
    assert cfg["max_level"] == 1
    assert set(cfg["never_allow"]) >= {"mdm", "agent"}
    assert cfg["overrides"]["allow_registered_without_mdm"] is False, (
        "예외는 기본 꺼짐이어야 합니다. 켜는 것은 사용자가 근거를 적고 하는 결정입니다."
    )
    assert cfg["watch_promotion"] is True
