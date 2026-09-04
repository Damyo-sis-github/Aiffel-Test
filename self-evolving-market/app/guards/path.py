"""#34 저장소가 동기화 폴더 안에 있으면 실행 거부 (§11.8 1·2번)."""

from __future__ import annotations

import os
from pathlib import Path

from app.config import runtime
from app.guards.base import GuardResult
from app.paths import repo_root


def _normalize(p: Path) -> str:
    """비교용 정규화: 구분자를 '\\' 로 통일하고 앞뒤에 구분자를 붙인다."""
    return "\\" + str(p).replace("/", "\\").strip("\\").lower() + "\\"


def sync_roots_from_env() -> list[str]:
    """OneDrive 환경변수로 실제 동기화 루트를 잡는다 (감사 결과의 onedrive.syncRoots 대응)."""
    out = []
    for key in os.environ:
        if key.upper().startswith("ONEDRIVE") and (v := os.environ[key]):
            out.append(v)
    return out


def path_guard(root: Path | None = None) -> GuardResult:
    root = root or repo_root()
    # 실제 존재하는 경로만 resolve 한다. Windows 경로 문자열을 다른 OS 에서 resolve 하면
    # cwd 가 앞에 붙어 검사가 무력화된다(테스트에서 실제로 발생했다).
    if root.exists():
        root = root.resolve()
    cfg = runtime()
    fragments = [str(f) for f in (cfg.get("forbidden_path_fragments") or [])]
    norm = _normalize(root)
    details = {"root": str(root), "onedrive_env": sync_roots_from_env()}

    for frag in fragments:
        needle = frag.replace("/", "\\").strip("\\").lower()
        if not needle:
            continue
        if f"\\{needle}\\" in norm:
            expected = (cfg.get("paths") or {}).get("expected_repo_root_windows", r"C:\dev\quant")
            return GuardResult(
                "path",
                False,
                f"저장소가 동기화/계정 폴더 안에 있습니다 ('{frag}'): {root}. "
                f"{expected} 같은 비동기화 경로로 옮기십시오.",
                details,
            )

    for sync_root in sync_roots_from_env():
        sr = _normalize(Path(sync_root))
        if norm.startswith(sr):
            return GuardResult("path", False, f"저장소가 OneDrive 동기화 루트 하위입니다: {sync_root}", details)

    return GuardResult("path", True, f"비동기화 경로: {root}", details)
