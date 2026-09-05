"""#34 저장소가 동기화 폴더 안에 있으면 실행 거부 (§11.8 1·2번).

실제 환경에서 두 번 뚫렸던 자리다. 둘 다 "정확히 일치"를 기대한 것이 원인이었다.

  1. `C:\\Users\\user\\OneDrive - 회사명\\...`
     OneDrive for Business 의 폴더명은 `OneDrive` 가 아니라 `OneDrive - <테넌트명>` 이다.
     경로 세그먼트 완전 일치로 찾으면 못 잡는다. → **세그먼트 안 부분 일치**로 바꿨다.

  2. `C:\\Users\\user\\문서\\...`, `C:\\Users\\user\\바탕 화면\\...`
     한국어 Windows 는 셸 표시명이 아니라 **실제 폴더명 자체가 한글**인 경우가 있다.
     영어 이름만 막으면 한국어 환경에서 전부 통과한다. → 한글 이름을 목록에 넣었다.

이 가드는 fail-closed 다. 애매하면 거부한다. 오탐이 나면 저장소를 옮기면 되지만,
미탐이 나면 회사 테넌트에 파일이 올라간 뒤이고 되돌릴 수 없다.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.config import runtime
from app.guards.base import GuardResult
from app.paths import repo_root


def _segments(p: Path) -> list[str]:
    """경로를 소문자 세그먼트 목록으로. 구분자는 / 와 \\ 둘 다 처리한다."""
    raw = str(p).replace("/", "\\")
    return [s.strip().lower() for s in raw.split("\\") if s.strip()]


def _normalize(p: Path) -> str:
    """접두사 비교용 정규화."""
    return "\\" + str(p).replace("/", "\\").strip("\\").lower() + "\\"


def sync_roots_from_env() -> list[str]:
    """OneDrive 환경변수로 실제 동기화 루트를 잡는다.

    주의: 작업 스케줄러가 S4U 로 실행하면 이 환경변수가 없을 수 있다.
    그래서 이것만 믿으면 안 되고, 세그먼트 검사가 1차 방어여야 한다.
    """
    out = []
    for key in os.environ:
        if key.upper().startswith("ONEDRIVE") and (v := os.environ[key]):
            out.append(v)
    return out


def matched_fragment(root: Path, fragments: list[str]) -> str | None:
    """금지 조각이 경로 **세그먼트 안에 부분 문자열로** 있으면 그 조각을 돌려준다."""
    segs = _segments(root)
    for frag in fragments:
        needle = str(frag).replace("/", "\\").strip("\\").strip().lower()
        if not needle:
            continue
        if any(needle in seg for seg in segs):
            return str(frag)
    return None


def path_guard(root: Path | None = None) -> GuardResult:
    root = root or repo_root()
    # 실제 존재하는 경로만 resolve 한다. Windows 경로 문자열을 다른 OS 에서 resolve 하면
    # cwd 가 앞에 붙어 검사가 무력화된다(테스트에서 실제로 발생했다).
    if root.exists():
        root = root.resolve()
    cfg = runtime()
    fragments = [str(f) for f in (cfg.get("forbidden_path_fragments") or [])]
    details = {"root": str(root), "onedrive_env": sync_roots_from_env()}
    expected = (cfg.get("paths") or {}).get("expected_repo_root_windows", r"C:\dev\quant")

    if (hit := matched_fragment(root, fragments)) is not None:
        return GuardResult(
            "path",
            False,
            f"저장소가 동기화/계정 폴더 안에 있습니다 ('{hit}'): {root}. "
            f"{expected} 같은 비동기화 경로로 옮기십시오. "
            "이 경로에 두면 파일이 회사 테넌트로 업로드되고, 삭제해도 버전 기록·보존 정책에 남습니다.",
            details,
        )

    for sync_root in sync_roots_from_env():
        if _normalize(root).startswith(_normalize(Path(sync_root))):
            return GuardResult(
                "path", False,
                f"저장소가 OneDrive 동기화 루트 하위입니다: {sync_root}. {expected} 로 옮기십시오.",
                details,
            )

    return GuardResult("path", True, f"비동기화 경로: {root}", details)
