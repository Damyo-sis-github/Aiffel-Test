"""#33 회사 네트워크에서 실행 금지.

로그온 시 실행 모델은 회사에서 로그온해도 발동한다. 이 가드가 없으면
회사 네트워크에서 증권 API 를 호출하게 된다(§11.1).
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess

from app.config import allowed_networks
from app.guards.base import GuardResult

_PROXY_ENVS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY")
_SSID_RE = re.compile(r"^\s*SSID\s+\d+\s*:\s*(.+?)\s*$|^\s*SSID\s*:\s*(.+?)\s*$", re.MULTILINE)


def _run(cmd: list[str], timeout: float = 5.0) -> str:
    exe = shutil.which(cmd[0])
    if not exe:
        return ""
    try:
        out = subprocess.run(  # noqa: S603 - 고정 인자, 셸 미사용
            [exe, *cmd[1:]], capture_output=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return out.stdout.decode("utf-8", "replace") + out.stderr.decode("utf-8", "replace")


def current_ssid() -> str | None:
    """연결된 Wi-Fi SSID. 얻을 수 없으면 None."""
    system = platform.system()
    if system == "Windows":
        txt = _run(["netsh", "wlan", "show", "interfaces"])
        for m in _SSID_RE.finditer(txt):
            val = (m.group(1) or m.group(2) or "").strip()
            if val and not val.upper().startswith("BSSID"):
                return val
        return None
    if system == "Darwin":
        txt = _run(["/usr/sbin/networksetup", "-getairportnetwork", "en0"])
        if ":" in txt:
            return txt.split(":", 1)[1].strip() or None
        return None
    txt = _run(["iwgetid", "-r"]) or _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
    for line in txt.splitlines():
        line = line.strip()
        if line.startswith("yes:"):
            return line.split(":", 1)[1] or None
        if line and ":" not in line:
            return line
    return None


def active_proxies() -> dict[str, str]:
    return {k: v for k in _PROXY_ENVS if (v := os.environ.get(k))}


def dns_suffixes() -> list[str]:
    out = []
    for key in ("USERDNSDOMAIN", "DNSDOMAIN"):
        if v := os.environ.get(key):
            out.append(v.lower())
    if os.path.exists("/etc/resolv.conf"):
        try:
            with open("/etc/resolv.conf", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith(("search", "domain")):
                        out.extend(tok.lower() for tok in line.split()[1:])
        except OSError:
            pass
    return out


def vpn_active() -> bool:
    if platform.system() == "Windows":
        txt = _run(["netsh", "interface", "show", "interface"]).lower()
        return any(k in txt for k in ("vpn", "wireguard", "openvpn", "tailscale"))
    txt = _run(["ip", "-o", "link", "show"]).lower()
    return any(k in txt for k in ("tun0", "wg0", "ppp0", "utun", "tailscale"))


def network_guard() -> GuardResult:
    cfg = allowed_networks()
    reject = cfg.get("reject_on") or {}
    ssid = current_ssid()
    details = {"ssid": ssid, "proxies": sorted(active_proxies()), "dns": dns_suffixes()}

    if reject.get("proxy_env", True) and (px := active_proxies()):
        return GuardResult("network", False, f"프록시 환경변수가 설정되어 있습니다: {sorted(px)}", details)

    corp = [str(s).lower() for s in (reject.get("corporate_dns_suffixes") or [])]
    if hit := next((c for c in corp for d in dns_suffixes() if c and c in d), None):
        return GuardResult("network", False, f"회사 DNS 접미사가 감지되었습니다: {hit}", details)

    if reject.get("vpn_adapter", True) and vpn_active():
        return GuardResult("network", False, "VPN 어댑터가 활성 상태입니다.", details)

    ssids = [str(s) for s in (cfg.get("ssids") or [])]
    macs = [str(m).lower() for m in (cfg.get("gateway_macs") or [])]
    if not ssids and not macs:
        return GuardResult(
            "network",
            False,
            "allowed_networks.yaml 의 ssids 가 비어 있습니다. 집 Wi-Fi SSID 를 등록하십시오. "
            f"(현재 SSID: {ssid or '알 수 없음'}) (§17 결정 대기: 집 Wi-Fi SSID)",
            details,
        )

    if ssid is None:
        if macs:
            return GuardResult("network", True, "SSID 없음 — 유선 게이트웨이 MAC 허용 경로", details)
        return GuardResult("network", False, "SSID 를 확인할 수 없고 허용 게이트웨이 MAC 도 없습니다.", details)

    if ssid not in ssids:
        return GuardResult("network", False, f"등록되지 않은 네트워크입니다: {ssid}", details)

    return GuardResult("network", True, f"집 네트워크: {ssid}", details)
