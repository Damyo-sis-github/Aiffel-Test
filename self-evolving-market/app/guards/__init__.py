"""실행 전 가드. 하나라도 어긋나면 실행하지 않고 사유를 남긴다 (§3.7, §11.1, §11.8).

가드 목록
  host      #31 회사 장비에서 실행 금지 (allowed_hosts.yaml)
  network   #33 회사 네트워크에서 실행 금지 (allowed_networks.yaml)
  path      #34 저장소가 동기화 폴더 안에 있으면 거부
  clock     #23 PC 시계 오차 > 60s 이면 중단
  lock      #6  protected.lock 해시 불일치면 거부
  llm_window #29 낮 시간 evolve 거부·이월
"""

from app.guards.base import GuardResult, GuardViolation, run_guards
from app.guards.clock import clock_guard
from app.guards.device import device_guard
from app.guards.host import host_guard
from app.guards.llm_window import llm_window_guard, next_allowed_llm_time
from app.guards.network import network_guard
from app.guards.path import path_guard
from app.guards.protected import protected_lock_guard

# daily/evolve 시작 시 이 순서로 돈다. 하나라도 거부면 실행하지 않는다.
DAILY_GUARDS = (
    host_guard,
    network_guard,
    path_guard,
    device_guard,
    clock_guard,
    protected_lock_guard,
)

__all__ = [
    "DAILY_GUARDS",
    "GuardResult",
    "GuardViolation",
    "clock_guard",
    "device_guard",
    "host_guard",
    "llm_window_guard",
    "network_guard",
    "next_allowed_llm_time",
    "path_guard",
    "protected_lock_guard",
    "run_guards",
]
