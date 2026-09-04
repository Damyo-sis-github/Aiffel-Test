"""#23 PC 시계 오차. NTP 오차 > 60s 면 중단 (§4.3 서버 시계)."""

from __future__ import annotations

import os
import socket
import struct
import time

from app.config import runtime
from app.guards.base import GuardResult

_NTP_EPOCH_DELTA = 2_208_988_800  # 1900 → 1970
_OVERRIDE_ENV = "QUANT_CLOCK_DRIFT_OVERRIDE"  # 테스트용 오차 주입


def ntp_offset_seconds(server: str, timeout: float = 3.0) -> float | None:
    """로컬 시계 − NTP 시계 (초). 실패 시 None."""
    packet = b"\x1b" + 47 * b"\0"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            t0 = time.time()
            s.sendto(packet, (server, 123))
            data, _ = s.recvfrom(48)
            t3 = time.time()
    except (TimeoutError, OSError):
        return None
    if len(data) < 48:
        return None
    t1 = struct.unpack("!I", data[32:36])[0] - _NTP_EPOCH_DELTA
    t2 = struct.unpack("!I", data[40:44])[0] - _NTP_EPOCH_DELTA
    # 왕복 지연을 뺀 로컬 오프셋
    return ((t0 - t1) + (t3 - t2)) / 2.0


def measure_drift() -> tuple[float | None, str]:
    if (inj := os.environ.get(_OVERRIDE_ENV)) is not None:
        return float(inj), "override"
    for server in runtime().get("clock", {}).get("ntp_servers", []):
        if (off := ntp_offset_seconds(str(server))) is not None:
            return off, str(server)
    return None, "unreachable"


def clock_guard() -> GuardResult:
    cfg = runtime().get("clock", {})
    limit = float(cfg.get("max_drift_seconds", 60))
    drift, source = measure_drift()
    details = {"drift_seconds": drift, "source": source, "limit": limit}

    if drift is None:
        # NTP 에 못 닿는 것은 오차가 크다는 증거가 아니다 → 통과시키되 플래그.
        return GuardResult("clock", True, "NTP 서버에 접근할 수 없어 시계 검사를 건너뜁니다(플래그).", details)
    if abs(drift) > limit:
        return GuardResult(
            "clock", False, f"시계 오차 {drift:+.1f}s 가 허용치 {limit:.0f}s 를 넘습니다 ({source}).", details
        )
    return GuardResult("clock", True, f"시계 오차 {drift:+.2f}s ({source})", details)
