"""저장소 경로 해석. 모든 상태는 디스크에 있다(설계 원칙 2)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

_ENV_ROOT = "QUANT_ROOT"


@lru_cache(maxsize=1)
def repo_root() -> Path:
    """저장소 루트. QUANT_ROOT 환경변수 > 이 파일 기준 상위 디렉터리."""
    if env := os.environ.get(_ENV_ROOT):
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def _sub(name: str, create: bool) -> Path:
    p = repo_root() / name
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def config_dir(create: bool = False) -> Path:
    return _sub("config", create)


def data_dir(create: bool = True) -> Path:
    return _sub("data", create)


def reports_dir(create: bool = True) -> Path:
    return _sub("reports", create)


def state_dir(create: bool = True) -> Path:
    return _sub("state", create)


def audit_dir(create: bool = True) -> Path:
    return _sub("audit", create)


def strategies_dir(create: bool = False) -> Path:
    return repo_root() / "app" / "strategies"


def meta_db_path() -> Path:
    """SQLite (메타·로그). §4.2"""
    return data_dir() / "meta.sqlite"


def parquet_dir(table: str) -> Path:
    p = data_dir() / "parquet" / table
    p.mkdir(parents=True, exist_ok=True)
    return p


def reset_cache() -> None:
    """테스트에서 QUANT_ROOT 를 바꾼 뒤 호출."""
    repo_root.cache_clear()
