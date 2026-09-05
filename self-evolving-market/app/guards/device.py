"""기기 가시성 가드 — daily/evolve 실행 전 검사 + **승격 감시**.

이 가드의 존재 이유는 한 문장이다:
  "오늘 레벨 2 인 기기는 내일 사용자 동의 없이 레벨 3 이 될 수 있다."

회사가 Intune 을 활성화하면 이미 등록된 기기는 MDM 으로 승격된다.
그 순간을 잡지 못하면, 파일·프로세스가 열린 줄도 모르고 계속 돌리게 된다.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import load_yaml
from app.guards.base import GuardResult
from app.guards.device_audit import DeviceAudit, Level, assess
from app.paths import state_dir

STATE_FILE = "device_audit.json"


def policy() -> dict:
    return load_yaml("device_policy.yaml")


def state_path() -> Path:
    return state_dir() / STATE_FILE


def read_last() -> dict | None:
    p = state_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_last(audit: DeviceAudit) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(audit.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def evaluate(audit: DeviceAudit, cfg: dict | None = None, last: dict | None = None) -> GuardResult:
    """감사 결과 + 정책 → 통과/거부. 순수 함수라 테스트할 수 있다."""
    cfg = cfg or policy()
    details = {"audit": audit.to_dict()}

    if not audit.checked:
        if cfg.get("skip_on_non_windows", True):
            return GuardResult("device", True, f"Windows 전용 검사 — {audit.platform} 에서는 건너뜁니다.", details)
        return GuardResult("device", False, f"{audit.platform} 에서는 기기 감사를 할 수 없습니다.", details)

    # ---- 절대 불가 항목 먼저 (예외 승인으로도 못 넘는다)
    never = {str(x).lower() for x in (cfg.get("never_allow") or [])}
    if "mdm" in never and audit.has(Level.MDM):
        return GuardResult(
            "device", False,
            "MDM 등록이 감지되었습니다 (레벨 3). 설치된 앱 목록·정책·원격 스크립트가 관리자에게 열립니다. "
            "폴더 위치·네트워크 가드로 막을 수 없습니다. 다른 기기를 쓰십시오.",
            details,
        )
    if "agent" in never and audit.has(Level.AGENT):
        agents = [f.detail for f in audit.findings if f.level is Level.AGENT]
        return GuardResult(
            "device", False,
            "EDR/문서보안 에이전트가 감지되었습니다 (레벨 4): " + "; ".join(agents[:3]) + ". "
            "프로세스 실행 기록과 파일 접근이 수집됩니다. 이 프로그램의 안전장치가 전부 무의미해집니다.",
            details,
        )

    # ---- 승격 감시: 지난 실행보다 레벨이 올라갔는가
    if cfg.get("watch_promotion", True) and last:
        before = int(last.get("level", 0))
        if int(audit.level) > before:
            return GuardResult(
                "device", False,
                f"기기 가시성 레벨이 {before} → {int(audit.level)} 로 **올라갔습니다** "
                f"({audit.level.label}). 회사 정책이 바뀌었을 수 있습니다. "
                "무엇이 바뀌었는지 확인하기 전에는 실행하지 않습니다. "
                "`quant audit --device` 로 상세를 보고, 계속하려면 정책을 다시 승인하십시오.",
                {**details, "previous_level": before},
            )

    # ---- 레벨 상한
    max_level = int(cfg.get("max_level", 1))
    over = cfg.get("overrides") or {}
    if int(audit.level) <= max_level:
        return GuardResult("device", True, f"기기 감사 레벨 {int(audit.level)} ({audit.level.label})", details)

    if audit.level is Level.REGISTERED and over.get("allow_registered_without_mdm"):
        who = over.get("acknowledged_by") or "미기재"
        when = over.get("acknowledged_at") or "미기재"
        return GuardResult(
            "device", True,
            f"레벨 2(기기 등록) — 정책 예외로 허용됨 (승인: {who}, {when}). "
            "MDM 승격 시 자동으로 중단됩니다.",
            {**details, "override": True},
        )

    return GuardResult(
        "device", False,
        f"기기 감사 레벨 {int(audit.level)} ({audit.level.label}) 이 상한 {max_level} 을 넘습니다. "
        "허용하려면 config/device_policy.yaml 의 overrides 를 사람이 명시적으로 승인해야 합니다 (§11.8).",
        details,
    )


def device_guard() -> GuardResult:
    audit = assess()
    result = evaluate(audit, policy(), read_last())
    # 통과했을 때만 기준선을 갱신한다. 거부 상태를 새 기준선으로 삼으면
    # 다음 실행에서 "승격 없음"이 되어 경보가 한 번만 울리고 사라진다.
    if result.ok and audit.checked:
        write_last(audit)
    return result
