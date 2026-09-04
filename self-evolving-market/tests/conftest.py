"""테스트 공통 픽스처.

원칙: 테스트는 네트워크를 쓰지 않는다. synthetic 소스로 전부 돌아간다.
저장소 루트를 tmp 로 옮겨 실제 data/ 를 건드리지 않는다.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
COPY_DIRS = ("config",)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO


@pytest.fixture()
def sandbox(tmp_path, monkeypatch) -> Path:
    """격리된 QUANT_ROOT. config 는 실제 것을 복사해 쓴다."""
    for d in COPY_DIRS:
        shutil.copytree(REPO / d, tmp_path / d)
    monkeypatch.setenv("QUANT_ROOT", str(tmp_path))
    monkeypatch.setenv("QUANT_GUARD_BYPASS", "1")

    import app.config as cfg
    import app.paths as paths
    import app.universe.snapshot as snap

    paths.reset_cache()
    cfg._load_yaml_cached.cache_clear()
    snap.symbol_meta.cache_clear()
    yield tmp_path
    paths.reset_cache()
    cfg._load_yaml_cached.cache_clear()
    snap.symbol_meta.cache_clear()


@pytest.fixture()
def seeded(sandbox):
    """2018~2020 데이터가 적재된 샌드박스."""
    from app.data.ingest import Ingestor
    from app.universe.snapshot import UniverseBuilder

    end = dt.date(2020, 12, 31)
    ing = Ingestor()
    for market in ("US", "KR"):
        ing.ingest_prices(market, dt.date(2015, 1, 1), end)
    ing.ingest_macro(dt.date(2015, 1, 1), end)
    ing.ingest_fx(dt.date(2015, 1, 1), end)
    UniverseBuilder().write(dt.date(2020, 11, 30))
    return sandbox


@pytest.fixture()
def locked(sandbox):
    from app.config import update_lock

    update_lock(True, "테스트")
    return sandbox


def env_without(monkeypatch, *names: str) -> None:
    for n in names:
        monkeypatch.delenv(n, raising=False)


@pytest.fixture()
def clean_proxy_env(monkeypatch):
    for n in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.delenv(n, raising=False)
    yield


os.environ.setdefault("PYTHONUTF8", "1")
