"""로컬 정적 대시보드. 읽기 전용이고, 오프라인이며, 두 테마에서 같은 코드로 그려진다.

여기 테스트 중 두 개는 **눈으로 보고서야** 발견한 버그의 재발 방지용이다.
스크린샷을 찍어보기 전까지 파이썬 테스트는 전부 통과하고 있었다.
"""

from __future__ import annotations

import datetime as dt
import json
import re

from app.data.meta_db import MetaDB
from app.reports import dashboard, dashboard_data


def _html(sandbox) -> str:
    dashboard.write(dt.date(2020, 12, 31))
    return (sandbox / "reports" / "dashboard.html").read_text(encoding="utf-8")


# ------------------------------------------------------------ 오프라인·읽기 전용
def test_no_external_requests(sandbox):
    """CDN·폰트·이미지 어떤 외부 요청도 없어야 한다. 외부 요청은 흔적을 남긴다 (§11.8)."""
    html = _html(sandbox)
    assert not re.search(r'(src|href)\s*=\s*["\']https?://', html)
    assert "cdn" not in html.lower().split("<script")[0]


def test_has_no_write_path(sandbox):
    """대시보드에서 게이트를 통과시키거나 상태를 바꿀 수 있는 경로가 없어야 한다."""
    html = _html(sandbox)
    for forbidden in ("<form", "fetch(", "XMLHttpRequest", "localStorage.setItem"):
        assert forbidden not in html, forbidden


# ------------------------------------------------------------ 다크 모드 회귀
def test_svg_colors_are_not_baked_into_attributes(sandbox):
    """SVG 색을 렌더 시점에 속성으로 구우면 테마 토글이 따라오지 않는다.

    실제로 다크로 바꿨을 때 라이트의 밝은 회색 그리드(#e1e0d9)가 그대로 남아
    검은 배경 위에서 흰 선으로 튀었다. 색은 클래스로만 준다.
    """
    html = _html(sandbox)
    js = html.split("<script>")[-1]
    # getComputedStyle 로 토큰을 읽어 속성에 넣는 패턴이 되살아나면 실패한다.
    assert "getComputedStyle" not in js
    for cls in ("gridline", "axisline", "t-muted", "t-ink", "ring", "fl-seq"):
        assert f".{cls}{{" in html or f".{cls} " in html, cls


def test_theme_tokens_live_on_root(sandbox):
    """토큰이 .dash 에만 있으면 body 의 var(--ink) 가 해석되지 않아 본문이 안 보인다."""
    css = _html(sandbox)
    assert ":root{" in css.replace(" ", "")
    assert ':root[data-theme="dark"]' in css          # 토글이 시스템 설정을 이긴다
    assert "prefers-color-scheme:dark" in css.replace(" ", "")


# ------------------------------------------------------------ 데이터 계약
def test_builds_on_empty_db(sandbox):
    """거래도 NAV 도 없는 첫날에도 죽지 않아야 한다."""
    data = dashboard_data.build(dt.date(2020, 12, 31), db=MetaDB())
    assert data["headline"]["closed_trades"] == 0
    assert data["nav"]["series"] == []
    json.loads(dashboard_data.to_json(data))          # allow_nan=False 로 직렬화 가능


def test_win_rate_never_ships_without_n_and_ci(sandbox):
    """#5 성과 수치엔 n 과 95% CI 병기. 승률만 단독으로 나가지 않는다."""
    data = dashboard_data.build(dt.date(2020, 12, 31), db=MetaDB())
    assert "win_ci" in data["headline"] and "closed_trades" in data["headline"]
    for s in data["strategies"]:
        if s["win_rate"] is not None:
            assert s["n"] and s["ci_low"] is not None and s["ci_high"] is not None


def test_w_pred_carries_b_pred_slot(sandbox):
    """#25 W_pred 는 B_pred 와 함께만 읽힌다. 자리를 비워두더라도 키는 항상 있다."""
    data = dashboard_data.build(dt.date(2020, 12, 31), db=MetaDB())
    for p in data["predictions"]:
        assert "b_pred" in p
