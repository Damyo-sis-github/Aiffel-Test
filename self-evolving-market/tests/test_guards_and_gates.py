"""#6 평가기 오염 · #16 실계좌 주문 · #17 시크릿 · #23 시계 · #26 이해충돌 ·
#29 LLM 창 · #31 회사 장비 · #33 회사 네트워크 · #34 동기화 폴더 · G1 킬스위치.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from app.config import (
    ProtectedLockError,
    check_protected_lock,
    compute_protected_hash,
    require_protected_lock,
    update_lock,
)
from app.guards.base import GuardResult, GuardViolation, run_guards
from app.guards.clock import clock_guard
from app.guards.host import host_guard
from app.guards.llm_window import in_blocked_window, llm_window_guard, next_allowed_llm_time
from app.guards.network import network_guard
from app.guards.path import path_guard
from app.paths import config_dir

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- #6 protected.lock
def test_lock_requires_human_ack(sandbox):
    with pytest.raises(ProtectedLockError, match="사람 승인"):
        update_lock(False, "자동 갱신 시도")


def test_lock_detects_gate_tampering(sandbox):
    update_lock(True, "초기")
    assert check_protected_lock().ok
    p = config_dir() / "gates.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    data["common"]["win_rate_min"] = 0.10          # 기준을 낮추려는 시도
    p.write_text(yaml.safe_dump(data), encoding="utf-8")

    from app.config import _load_yaml_cached

    _load_yaml_cached.cache_clear()
    st = check_protected_lock()
    assert not st.ok
    assert "불일치" in st.message
    with pytest.raises(ProtectedLockError):
        require_protected_lock()


def test_lock_covers_evaluator_directory(sandbox):
    from app.config import PROTECTED_TARGETS

    assert "app/evaluator" in PROTECTED_TARGETS
    assert "config/gates.yaml" in PROTECTED_TARGETS
    assert "config/risk.yaml" in PROTECTED_TARGETS


def test_protected_hash_is_stable(sandbox):
    assert compute_protected_hash() == compute_protected_hash()


# ---------------------------------------------------------------- #31 기기
def test_host_guard_rejects_empty_allowlist(sandbox, monkeypatch):
    monkeypatch.delenv("QUANT_GUARD_BYPASS", raising=False)
    r = host_guard()
    assert not r.ok and "비어 있습니다" in r.reason


def test_host_guard_rejects_unregistered_host(sandbox, monkeypatch):
    monkeypatch.delenv("QUANT_GUARD_BYPASS", raising=False)
    p = config_dir() / "allowed_hosts.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    cfg["hostnames"] = ["MY-LAPTOP"]
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    from app.config import _load_yaml_cached

    _load_yaml_cached.cache_clear()
    monkeypatch.setattr("app.guards.host.current_hostname", lambda: "COMPANY-PC")
    r = host_guard()
    assert not r.ok and "등록되지 않은 기기" in r.reason


def test_host_guard_rejects_corporate_pattern(sandbox, monkeypatch):
    monkeypatch.delenv("QUANT_GUARD_BYPASS", raising=False)
    p = config_dir() / "allowed_hosts.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    cfg["hostnames"] = ["corp-laptop"]
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    from app.config import _load_yaml_cached

    _load_yaml_cached.cache_clear()
    monkeypatch.setattr("app.guards.host.current_hostname", lambda: "corp-laptop")
    assert not host_guard().ok


def test_host_guard_accepts_registered(sandbox, monkeypatch):
    monkeypatch.delenv("QUANT_GUARD_BYPASS", raising=False)
    p = config_dir() / "allowed_hosts.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    cfg["hostnames"] = ["my-laptop"]
    cfg["reject_if_domain_joined"] = False
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    from app.config import _load_yaml_cached

    _load_yaml_cached.cache_clear()
    monkeypatch.setattr("app.guards.host.current_hostname", lambda: "MY-LAPTOP")
    monkeypatch.setattr("app.guards.host.domain_suffix", lambda: "")
    assert host_guard().ok


# ---------------------------------------------------------------- #33 네트워크
def test_network_guard_rejects_proxy(sandbox, monkeypatch):
    monkeypatch.delenv("QUANT_GUARD_BYPASS", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp.local:8080")
    r = network_guard()
    assert not r.ok and "프록시" in r.reason


def test_network_guard_rejects_empty_ssid_list(sandbox, monkeypatch, clean_proxy_env):
    monkeypatch.delenv("QUANT_GUARD_BYPASS", raising=False)
    monkeypatch.setattr("app.guards.network.current_ssid", lambda: "SOME_WIFI")
    monkeypatch.setattr("app.guards.network.vpn_active", lambda: False)
    monkeypatch.setattr("app.guards.network.dns_suffixes", lambda: [])
    r = network_guard()
    assert not r.ok and "비어 있습니다" in r.reason


def test_network_guard_rejects_unknown_ssid(sandbox, monkeypatch, clean_proxy_env):
    monkeypatch.delenv("QUANT_GUARD_BYPASS", raising=False)
    p = config_dir() / "allowed_networks.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    cfg["ssids"] = ["MY_HOME_5G"]
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    from app.config import _load_yaml_cached

    _load_yaml_cached.cache_clear()
    monkeypatch.setattr("app.guards.network.current_ssid", lambda: "COMPANY_WIFI")
    monkeypatch.setattr("app.guards.network.vpn_active", lambda: False)
    monkeypatch.setattr("app.guards.network.dns_suffixes", lambda: [])
    r = network_guard()
    assert not r.ok and "등록되지 않은 네트워크" in r.reason


def test_network_guard_rejects_corporate_dns(sandbox, monkeypatch, clean_proxy_env):
    monkeypatch.delenv("QUANT_GUARD_BYPASS", raising=False)
    monkeypatch.setattr("app.guards.network.dns_suffixes", lambda: ["corp.local"])
    r = network_guard()
    assert not r.ok and "DNS" in r.reason


# ---------------------------------------------------------------- #34 경로
@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\me\OneDrive\quant",
        r"C:\Users\me\Documents\quant",
        r"C:\Users\me\Desktop\quant",
        "/home/me/Dropbox/quant",
    ],
)
def test_path_guard_rejects_synced_folders(sandbox, monkeypatch, path):
    monkeypatch.delenv("QUANT_GUARD_BYPASS", raising=False)
    r = path_guard(Path(path))
    assert not r.ok


def test_path_guard_accepts_clean_path(sandbox, monkeypatch):
    monkeypatch.delenv("QUANT_GUARD_BYPASS", raising=False)
    assert path_guard(Path(r"C:\dev\quant")).ok


def test_path_guard_rejects_onedrive_env_root(sandbox, monkeypatch):
    monkeypatch.delenv("QUANT_GUARD_BYPASS", raising=False)
    monkeypatch.setenv("OneDrive", r"C:\Users\me\SyncedRoot")
    r = path_guard(Path(r"C:\Users\me\SyncedRoot\quant"))
    assert not r.ok


# ---------------------------------------------------------------- #23 시계
def test_clock_guard_rejects_large_drift(sandbox, monkeypatch):
    monkeypatch.delenv("QUANT_GUARD_BYPASS", raising=False)
    monkeypatch.setenv("QUANT_CLOCK_DRIFT_OVERRIDE", "180")
    r = clock_guard()
    assert not r.ok and "시계 오차" in r.reason


def test_clock_guard_accepts_small_drift(sandbox, monkeypatch):
    monkeypatch.setenv("QUANT_CLOCK_DRIFT_OVERRIDE", "3.5")
    assert clock_guard().ok


# ---------------------------------------------------------------- 가드 조합
def test_run_guards_is_fail_closed(sandbox, monkeypatch):
    monkeypatch.delenv("QUANT_GUARD_BYPASS", raising=False)

    def exploding() -> GuardResult:
        raise RuntimeError("가드 내부 오류")

    with pytest.raises(GuardViolation):
        run_guards([exploding])


def test_bypass_marks_results(sandbox, monkeypatch):
    monkeypatch.setenv("QUANT_GUARD_BYPASS", "1")
    monkeypatch.setattr("app.guards.host.current_hostname", lambda: "UNKNOWN")
    results = run_guards([host_guard])
    assert results[0].ok and results[0].bypassed


# ---------------------------------------------------------------- #29 LLM 창
def test_llm_window_blocks_weekday_noon(sandbox):
    noon = dt.datetime(2026, 9, 2, 12, 0)          # 수요일
    blocked, why = in_blocked_window(noon)
    assert blocked and "08:00" in why
    r = llm_window_guard(noon)
    assert not r.ok and "이월" in r.reason
    assert next_allowed_llm_time(noon).hour == 19


def test_llm_window_allows_evening(sandbox):
    evening = dt.datetime(2026, 9, 2, 19, 30)
    assert not in_blocked_window(evening)[0]
    assert llm_window_guard(evening).ok


def test_llm_window_allows_weekend(sandbox):
    saturday = dt.datetime(2026, 9, 5, 12, 0)
    assert not in_blocked_window(saturday)[0]


def test_llm_window_force_overrides(sandbox):
    noon = dt.datetime(2026, 9, 2, 12, 0)
    r = llm_window_guard(noon, force=True)
    assert r.ok and r.details.get("forced")


# ---------------------------------------------------------------- #26 이해충돌
def test_excluded_symbol_gets_no_signal(sandbox):
    p = config_dir() / "exclusions.yaml"
    p.write_text(yaml.safe_dump({"symbols": [{"symbol": "AAPL", "market": "US", "reason": "협력사"}],
                                 "name_patterns": []}), encoding="utf-8")
    from app.config import _load_yaml_cached
    from app.universe.exclusions import load_exclusions

    _load_yaml_cached.cache_clear()
    excl = load_exclusions()
    assert excl.excludes("AAPL", "US")
    assert not excl.excludes("MSFT", "US")


def test_excluded_symbol_absent_from_universe(sandbox):
    p = config_dir() / "exclusions.yaml"
    p.write_text(yaml.safe_dump({"symbols": [{"symbol": "AAPL", "market": "US"}], "name_patterns": []}),
                 encoding="utf-8")
    from app.config import _load_yaml_cached

    _load_yaml_cached.cache_clear()
    from app.data.ingest import Ingestor
    from app.universe.snapshot import UniverseBuilder

    ing = Ingestor()
    ing.ingest_prices("US", dt.date(2020, 1, 1), dt.date(2020, 6, 30))
    df = UniverseBuilder().build(dt.date(2020, 6, 30))
    assert "AAPL" not in set(df["symbol"])


def test_exclusions_file_must_exist(sandbox):
    (config_dir() / "exclusions.yaml").unlink()
    from app.config import ConfigError, _load_yaml_cached, exclusions

    _load_yaml_cached.cache_clear()
    with pytest.raises(ConfigError):
        exclusions()


# ---------------------------------------------------------------- G1 킬스위치
def test_killswitch_trips_and_requires_ack(sandbox):
    from app.portfolio.killswitch import KillSwitch

    ks = KillSwitch()
    assert not ks.tripped
    st = ks.evaluate(0.20, "ACC_L", dt.date(2020, 6, 1))
    assert st.tripped and "킬스위치 발동" in st.message()
    assert KillSwitch().tripped
    with pytest.raises(PermissionError, match="사람 승인"):
        KillSwitch().resume(False)
    KillSwitch().resume(True, "테스트 승인")
    assert not KillSwitch().tripped


def test_killswitch_does_not_trip_below_threshold(sandbox):
    from app.portfolio.killswitch import KillSwitch

    assert not KillSwitch().evaluate(0.10, "ACC_L").tripped


def test_corrupt_killswitch_state_is_fail_closed(sandbox):
    from app.paths import state_dir
    from app.portfolio.killswitch import KillSwitch

    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / "killswitch.json").write_text("{깨진 json", encoding="utf-8")
    assert KillSwitch().tripped
