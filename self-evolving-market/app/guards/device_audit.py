"""기기 가시성 감사 (§11.8 전제, §17 "기기 감사 레벨").

무엇을 재는가: **회사가 이 기기에서 무엇을 볼 수 있는가.**

레벨 정의 (낮을수록 안전, 위로 갈수록 폴더 위치·네트워크로 막을 수 없어진다)

  0 CLEAN       아무 연결 없음
  1 SYNC        OneDrive/KFM 등 계정 경로 동기화
                → 보이는 것: **그 폴더 안의 파일 전체**
                → 막는 법: 저장소를 동기화 폴더 밖에 둔다 (#34 path_guard)
  2 REGISTERED  기기가 회사 계정에 등록됨 (WorkplaceJoined / AzureAdJoined / DomainJoined)
                → 보이는 것: 기기명·OS·모델·로그인 시각 등 **메타데이터**
                → 안 보이는 것: 파일, 프로세스, 앱 목록 (MDM 이 없다면)
                → 막는 법: 없음. 폴더 위치와 무관하다.
  3 MDM         Intune 등 MDM 등록 (MdmUrl 존재)
                → 보이는 것: 설치된 앱 목록, 정책 준수 상태, 원격 스크립트 실행 가능
  4 AGENT       EDR / 문서보안(DLP·DRM) 에이전트
                → 보이는 것: **프로세스 실행 기록, 커맨드라인 인자, 파일 접근 로그**
                → 이 레벨에서는 이 프로그램의 모든 안전장치가 무의미하다.

핵심 위험: **레벨 2 는 조용히 레벨 3 이 될 수 있다.**
회사가 Intune 을 활성화하면 이미 등록된 기기는 사용자 동의 없이 MDM 으로 승격된다.
그래서 이 감사는 daily 마다 돌고, 승격이 감지되면 실행을 멈춘다.

Windows 전용이다. 다른 OS 에서는 검사를 건너뛴다 — 실집행은 allowed_hosts 에
등록된 그 Windows 노트북에서 일어나기 때문이다.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import IntEnum

DSREG_TIMEOUT = 20.0
PS_TIMEOUT = 60.0


class Level(IntEnum):
    CLEAN = 0
    SYNC = 1
    REGISTERED = 2
    MDM = 3
    AGENT = 4

    @property
    def label(self) -> str:
        return {
            Level.CLEAN: "연결 없음",
            Level.SYNC: "계정 경로 동기화",
            Level.REGISTERED: "기기 등록",
            Level.MDM: "MDM 관리",
            Level.AGENT: "EDR/문서보안 에이전트",
        }[self]


@dataclass(frozen=True)
class Finding:
    level: Level
    key: str
    detail: str
    remedy: str = ""

    def __str__(self) -> str:
        head = f"[레벨 {int(self.level)} {self.level.label}] {self.key}: {self.detail}"
        return head + (f"\n      → {self.remedy}" if self.remedy else "")


@dataclass
class DeviceAudit:
    platform: str
    findings: list[Finding] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    checked: bool = True

    @property
    def level(self) -> Level:
        return max((f.level for f in self.findings), default=Level.CLEAN)

    def has(self, level: Level) -> bool:
        return any(f.level is level for f in self.findings)

    def render(self) -> str:
        if not self.checked:
            return f"기기 감사: 건너뜀 ({self.platform} — Windows 전용 검사)"
        lines = [f"기기 감사 결과: **레벨 {int(self.level)} ({self.level.label})**", ""]
        if not self.findings:
            lines.append("  발견 없음 — 회사가 이 기기에서 볼 수 있는 것이 없습니다.")
        for f in sorted(self.findings, key=lambda x: -int(x.level)):
            lines.append("  " + str(f))
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "checked": self.checked,
            "level": int(self.level),
            "level_label": self.level.label,
            "findings": [
                {"level": int(f.level), "key": f.key, "detail": f.detail, "remedy": f.remedy}
                for f in self.findings
            ],
        }


# ---------------------------------------------------------------- dsregcmd


def parse_dsregcmd(text: str) -> dict[str, list[str]]:
    """`dsregcmd /status` 출력을 key → [값들] 로 파싱한다.

    같은 키가 여러 섹션에 나올 수 있어(MdmUrl 은 Tenant Details 와 Work Account 양쪽)
    값을 리스트로 모은다. 값이 비어 있으면 빈 문자열이 들어간다.
    """
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        m = re.match(r"^\s{2,}([A-Za-z][A-Za-z0-9 _\-]*?)\s*:\s*(.*?)\s*$", line)
        if not m:
            continue
        key, value = m.group(1).strip(), m.group(2).strip()
        out.setdefault(key, []).append(value)
    return out


def _run(cmd: list[str], timeout: float) -> str:
    exe = shutil.which(cmd[0])
    if not exe:
        return ""
    try:
        p = subprocess.run(  # noqa: S603 - 고정 인자, 셸 미사용
            [exe, *cmd[1:]], capture_output=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")


def read_dsregcmd() -> dict[str, list[str]]:
    return parse_dsregcmd(_run(["dsregcmd", "/status"], DSREG_TIMEOUT))


def _yes(values: list[str] | None) -> bool:
    return bool(values) and any(v.strip().upper() == "YES" for v in values)


def _nonempty(values: list[str] | None) -> str | None:
    for v in values or []:
        if v.strip():
            return v.strip()
    return None


# ---------------------------------------------------------------- EDR / DLP

# 국내외 EDR·백신·문서보안(DRM/DLP) 제품. 소문자 부분 일치로 찾는다.
AGENT_PATTERNS: tuple[str, ...] = (
    # EDR / XDR (해외)
    "crowdstrike", "sentinelone", "carbon black", "carbonblack", "cortex xdr", "traps",
    "tanium", "cybereason", "trellix", "fireeye", "rapid7", "insight agent",
    # 백신 (해외)
    "mcafee", "symantec", "sophos", "eset", "kaspersky", "bitdefender", "trend micro",
    # 국내 백신 / 보안
    "ahnlab", "안랩", "v3 ", "hauri", "하우리", "inca", "잉카", "estsecurity", "이스트시큐리티",
    "alyac", "알약",
    # 문서보안 (DRM / DLP) — 국내 기업에 매우 흔하고 파일 접근을 후킹한다
    "fasoo", "파수", "softcamp", "소프트캠프", "markany", "마크애니", "jiran", "지란지교",
    "somansa", "소만사", "waterwall", "워터월", "docuware", "netsecure", "d.smart",
    "digital guardian", "forcepoint", "netskope", "zscaler",
)

# Microsoft Defender for Endpoint 온보딩 서비스. 이름이 짧아 별도 정확 일치로 본다.
DEFENDER_ATC_SERVICES = ("sense",)  # Defender for Endpoint 온보딩 서비스

_PS_INVENTORY = r"""
$ErrorActionPreference = 'SilentlyContinue'
$svc = Get-Service | Select-Object -Property Name, DisplayName, Status
$paths = @(
  'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
  'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
$apps = Get-ItemProperty $paths | Where-Object { $_.DisplayName } |
        Select-Object -Property DisplayName -Unique
[pscustomobject]@{ services = @($svc); apps = @($apps) } | ConvertTo-Json -Depth 3 -Compress
"""


def read_inventory() -> dict:
    """서비스·설치 프로그램 목록. PowerShell 1회 호출."""
    txt = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_INVENTORY], PS_TIMEOUT)
    if not txt.strip():
        return {}
    try:
        return json.loads(txt[txt.index("{") : txt.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return {}


def detect_agents(inventory: dict) -> list[tuple[str, str]]:
    """(제품 힌트, 근거) 목록. 비어 있으면 에이전트 미감지."""
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(pattern: str, evidence: str) -> None:
        if pattern not in seen:
            seen.add(pattern)
            hits.append((pattern, evidence))

    for svc in inventory.get("services") or []:
        name = str(svc.get("Name", ""))
        disp = str(svc.get("DisplayName", ""))
        blob = f"{name} {disp}".lower()
        for pat in AGENT_PATTERNS:
            if pat in blob:
                add(pat, f"서비스 '{disp or name}'")
        # Sense = Defender for Endpoint 온보딩. WinDefend 단독은 일반 백신이라 제외한다
        # (그걸로 거부하면 모든 Windows 가 막힌다).
        if name.lower() == "sense" and str(svc.get("Status", "")).lower() == "running":
            add("defender for endpoint", f"서비스 '{disp or name}' 실행 중")

    for app in inventory.get("apps") or []:
        disp = str(app.get("DisplayName", "")).lower()
        for pat in AGENT_PATTERNS:
            if pat in disp:
                add(pat, f"설치 프로그램 '{app.get('DisplayName')}'")
    return hits


# ---------------------------------------------------------------- 동기화

def detect_sync_roots() -> list[str]:
    """OneDrive 동기화 루트. `OneDrive` 와 `OneDriveCommercial` 이 같은 값을 가리키는 일이
    흔하므로 **중복을 제거**한다 (실제 출력에서 같은 경로가 두 번 찍혔다)."""
    import os

    seen: dict[str, str] = {}
    for key, value in sorted(os.environ.items()):
        if key.upper().startswith("ONEDRIVE") and value:
            seen.setdefault(value.rstrip("\\").lower(), value)
    return list(seen.values())


# 레지스트리 키 → 영어 기준 이름. 실제 폴더명은 로케일마다 다르므로
# (한국어 Windows 의 Pictures 는 '사진'이 아니라 '그림'이다) 경로에서 직접 읽는다.
KNOWN_FOLDER_KEYS = {
    "Personal": "Documents",
    "Desktop": "Desktop",
    "My Pictures": "Pictures",
    "My Video": "Videos",
    "My Music": "Music",
    "{374DE290-123F-4565-9164-39C4925E467B}": "Downloads",
}


def detect_kfm() -> list[tuple[str, str]]:
    """알려진 폴더가 OneDrive 로 리디렉션됐는지 (KFM).

    라벨은 **실제 폴더명**을 쓴다. 하드코딩한 한글 이름을 쓰면 로케일이 다를 때
    엉뚱한 이름이 찍힌다 — 실제로 'Pictures' 를 '사진'이라 찍었는데 폴더명은 '그림'이었다.
    """
    import os
    import winreg  # type: ignore[import-not-found]

    out: list[tuple[str, str]] = []
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as k:
            for name, english in KNOWN_FOLDER_KEYS.items():
                try:
                    value, _ = winreg.QueryValueEx(k, name)
                except OSError:
                    continue
                expanded = os.path.expandvars(str(value))
                if "onedrive" not in expanded.lower():
                    continue
                actual = expanded.replace("/", "\\").rstrip("\\").split("\\")[-1]
                label = f"{actual} ({english})" if actual and actual != english else english
                out.append((label, expanded))
    except OSError:
        return []
    return out


# ---------------------------------------------------------------- 종합


def assess() -> DeviceAudit:
    system = platform.system()
    if system != "Windows":
        return DeviceAudit(platform=system, checked=False)

    audit = DeviceAudit(platform=system)
    dsreg = read_dsregcmd()
    audit.raw["dsregcmd"] = {k: v for k, v in dsreg.items() if k in (
        "AzureAdJoined", "EnterpriseJoined", "DomainJoined", "WorkplaceJoined",
        "MdmUrl", "TenantName", "DeviceId",
    )}

    # ---- 레벨 2: 기기 등록
    if _yes(dsreg.get("AzureAdJoined")):
        audit.findings.append(Finding(
            Level.REGISTERED, "AzureAdJoined", "회사 소유 기기로 Azure AD 에 조인되어 있습니다.",
            "회사 소유 기기입니다. 이 프로그램을 여기서 돌리지 마십시오.",
        ))
    if _yes(dsreg.get("DomainJoined")):
        audit.findings.append(Finding(
            Level.REGISTERED, "DomainJoined", "온프레미스 도메인에 조인되어 있습니다.",
            "회사 관리 기기입니다. 다른 기기를 쓰십시오.",
        ))
    if _yes(dsreg.get("WorkplaceJoined")):
        audit.findings.append(Finding(
            Level.REGISTERED, "WorkplaceJoined",
            "개인 기기가 회사 계정에 등록(Azure AD Registered)되어 있습니다. "
            "관리자는 기기명·OS·모델·로그인 시각 등 메타데이터를 봅니다. "
            "MDM 이 없다면 파일·프로세스는 보지 못합니다.",
            "설정 → 계정 → 회사 또는 학교 액세스 → 연결 끊기 로 레벨 1 로 내릴 수 있습니다. "
            "유지한다면 MdmUrl 승격을 계속 감시해야 합니다.",
        ))

    # ---- 레벨 3: MDM
    if (mdm := _nonempty(dsreg.get("MdmUrl"))) is not None:
        audit.findings.append(Finding(
            Level.MDM, "MdmUrl", f"MDM 등록됨: {mdm}",
            "설치된 앱 목록·정책·원격 스크립트가 관리자에게 열립니다. 다른 기기를 쓰십시오.",
        ))

    # ---- 레벨 4: EDR / 문서보안
    inventory = read_inventory()
    for pattern, evidence in detect_agents(inventory):
        audit.findings.append(Finding(
            Level.AGENT, "보안 에이전트", f"{pattern} 감지 — {evidence}",
            "프로세스 실행 기록과 파일 접근이 수집됩니다. 폴더 위치·네트워크 가드가 무의미합니다.",
        ))

    # ---- 레벨 1: 동기화
    for root in detect_sync_roots():
        audit.findings.append(Finding(
            Level.SYNC, "OneDrive 동기화 루트", root,
            "이 경로 안의 파일은 회사 테넌트에 업로드되고, 삭제해도 버전 기록·보존 정책에 남습니다. "
            "저장소를 이 밖에 두면 (#34 가 강제) 영향이 없습니다.",
        ))
    for label, path in detect_kfm():
        audit.findings.append(Finding(
            Level.SYNC, f"KFM 활성 ({label})", path,
            f"'{label}' 폴더가 OneDrive 로 리디렉션되어 있습니다. 여기에 결과 파일을 복사하지 마십시오.",
        ))
    return audit
