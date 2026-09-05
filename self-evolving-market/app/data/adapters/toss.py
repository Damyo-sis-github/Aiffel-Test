"""토스증권 Open API 어댑터 — **읽기 전용**. §4.1 절대 규칙.

이 파일이 토스 API 로 나가는 유일한 통로다.
  - 허용 엔드포인트는 config/runtime.yaml 의 allowlist 하드 체크를 통과해야 한다.
  - 주문 계열 경로는 여기서도, 코드베이스 어디에서도 존재할 수 없다 (#16, CI 가 검사).
  - 자격증명은 .env 에서만 읽고 로그·리포트·예외 메시지에 절대 싣지 않는다 (#17).

발급 전(§17 결정 대기)이라 기본값은 enabled: false 이며, 이 상태에서 호출하면 예외다.
"""

from __future__ import annotations

import os
from typing import Any

from app.config import runtime


class TossDisabled(RuntimeError):
    pass


class TossEndpointRejected(RuntimeError):
    """allowlist 밖의 엔드포인트. 절대 호출하지 않는다."""


def _cfg() -> dict[str, Any]:
    return runtime().get("toss", {}) or {}


def allowed_endpoints() -> list[str]:
    return [str(e) for e in (_cfg().get("allowed_endpoints") or [])]


def assert_endpoint_allowed(path: str) -> str:
    """allowlist 검사. 이 함수를 거치지 않는 토스 호출은 존재해선 안 된다."""
    normalized = "/" + str(path).strip().lstrip("/").split("?", 1)[0]
    if normalized not in allowed_endpoints():
        raise TossEndpointRejected(
            f"허용되지 않은 토스 엔드포인트입니다: {normalized}. "
            f"허용 목록: {allowed_endpoints()} (§4.1 읽기 전용 스코프)"
        )
    return normalized


def _credentials() -> tuple[str, str]:
    """.env 에서만 읽는다. 값은 어떤 경로로도 출력하지 않는다."""
    key = os.environ.get("TOSS_API_KEY")
    secret = os.environ.get("TOSS_API_SECRET")
    if not key or not secret:
        raise TossDisabled("TOSS_API_KEY / TOSS_API_SECRET 가 .env 에 없습니다.")
    return key, secret


def get(path: str, params: dict[str, Any] | None = None, timeout: float = 10.0) -> Any:
    """시세·종목정보 GET. 그 외 HTTP 메서드는 이 모듈에 존재하지 않는다."""
    cfg = _cfg()
    if not cfg.get("enabled", False):
        raise TossDisabled("토스 API 는 아직 비활성입니다 (runtime.yaml: toss.enabled=false, §17).")
    endpoint = assert_endpoint_allowed(path)

    import httpx  # 선택 의존성

    key, secret = _credentials()
    url = str(cfg.get("base_url", "")).rstrip("/") + endpoint
    try:
        resp = httpx.get(
            url,
            params=params or {},
            headers={"X-API-KEY": key, "X-API-SECRET": secret, "Accept": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        # 자격증명이 URL·헤더로 새지 않도록 메시지를 직접 만든다 (#17).
        raise RuntimeError(f"토스 API 호출 실패: {endpoint} ({type(exc).__name__})") from None
