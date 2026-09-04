"""설정 로더. YAML 을 읽고 캐시하며, 보호 대상 파일의 해시를 계산한다.

§10.1: evaluator/, config/gates.yaml, config/risk.yaml 의 해시를 config/protected.lock 에
기록한다. `daily` 시작 시 불일치면 실행을 거부한다(#6).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.paths import config_dir, repo_root
from app.util.hashing import hash_paths

# §10.1 보호 대상. 사람 승인(G2) 없이는 바뀔 수 없다.
PROTECTED_TARGETS: tuple[str, ...] = (
    "app/evaluator",
    "config/gates.yaml",
    "config/risk.yaml",
)
LOCK_FILENAME = "protected.lock"


class ConfigError(RuntimeError):
    pass


class ProtectedLockError(RuntimeError):
    """protected.lock 해시 불일치. 실행을 거부한다."""


@lru_cache(maxsize=64)
def _load_yaml_cached(path_str: str, mtime_ns: int) -> str:
    with open(path_str, encoding="utf-8") as f:
        return json.dumps(yaml.safe_load(f) or {})


def load_yaml(name: str) -> dict[str, Any]:
    """config/<name> 로드. 존재하지 않으면 ConfigError."""
    path = config_dir() / name
    if not path.exists():
        raise ConfigError(f"설정 파일이 없습니다: {path}")
    return json.loads(_load_yaml_cached(str(path), path.stat().st_mtime_ns))


def gates() -> dict[str, Any]:
    return load_yaml("gates.yaml")


def risk() -> dict[str, Any]:
    return load_yaml("risk.yaml")


def costs() -> dict[str, Any]:
    """§7.3 비용 모델. 기본값 없음 — 누락 시 예외(#3)."""
    c = load_yaml("costs.yaml")
    for market in ("KR", "US"):
        m = c.get("markets", {}).get(market)
        if not m or "slippage_rate" not in m:
            raise ConfigError(f"costs.yaml 에 {market} 비용 모델이 없습니다. 기본값은 허용되지 않습니다.")
    return c


def runtime() -> dict[str, Any]:
    return load_yaml("runtime.yaml")


def etf_universe() -> dict[str, Any]:
    return load_yaml("etf_universe.yaml")


def themes() -> dict[str, Any]:
    return load_yaml("themes.yaml")


def exclusions() -> dict[str, Any]:
    return load_yaml("exclusions.yaml")


def alerts_cfg() -> dict[str, Any]:
    return load_yaml("alerts.yaml")


def llm_window() -> dict[str, Any]:
    return load_yaml("llm_window.yaml")


def allowed_hosts() -> dict[str, Any]:
    return load_yaml("allowed_hosts.yaml")


def allowed_networks() -> dict[str, Any]:
    return load_yaml("allowed_networks.yaml")


# ---------------------------------------------------------------- protected.lock


@dataclass(frozen=True)
class LockState:
    expected: str | None
    actual: str
    ok: bool
    targets: tuple[str, ...]

    @property
    def message(self) -> str:
        if self.ok:
            return f"protected.lock OK ({self.actual})"
        if self.expected is None:
            return (
                "protected.lock 이 없습니다. 최초 1회 `quant lock --update --ack` 로 봉인하십시오. "
                f"(현재 해시 {self.actual})"
            )
        return (
            f"protected.lock 해시 불일치: 기대 {self.expected} / 실제 {self.actual}. "
            "evaluator/ 또는 gates·risk 설정이 변경되었습니다. "
            "사람 승인이 필요합니다: `quant lock --update --ack`"
        )


def compute_protected_hash() -> str:
    return hash_paths(repo_root() / t for t in PROTECTED_TARGETS)


def lock_path() -> Path:
    return config_dir() / LOCK_FILENAME


def read_lock() -> str | None:
    p = lock_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))["hash"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise ProtectedLockError(f"protected.lock 파일이 손상되었습니다: {p}") from exc


def check_protected_lock() -> LockState:
    actual = compute_protected_hash()
    expected = read_lock()
    return LockState(expected=expected, actual=actual, ok=expected == actual, targets=PROTECTED_TARGETS)


def require_protected_lock() -> LockState:
    """`daily` 시작 시 호출. 불일치면 예외 (#6)."""
    st = check_protected_lock()
    if not st.ok:
        raise ProtectedLockError(st.message)
    return st


def update_lock(ack: bool, reason: str = "") -> LockState:
    """G2 사람 게이트. ack=False 면 갱신하지 않는다(#13: 자동화 금지)."""
    if not ack:
        raise ProtectedLockError(
            "protected.lock 갱신은 사람 승인이 필요합니다. `--ack` 없이는 갱신하지 않습니다 (G2)."
        )
    actual = compute_protected_hash()
    lock_path().write_text(
        json.dumps(
            {
                "hash": actual,
                "targets": list(PROTECTED_TARGETS),
                "reason": reason,
                "acked_by": os.environ.get("USERNAME") or os.environ.get("USER") or "unknown",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return LockState(expected=actual, actual=actual, ok=True, targets=PROTECTED_TARGETS)
