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


# ------------------------------------------------------------ 자동 갱신
def test_watch_adds_meta_refresh_and_default_has_none(sandbox):
    """--watch 없이는 페이지가 스스로 다시 읽지 않는다. 붙였을 때만 붙는다."""
    plain = dashboard.render(dashboard_data.build(dt.date(2020, 12, 31), db=MetaDB()))
    assert "http-equiv=\"refresh\"" not in plain

    watched = dashboard.render(dashboard_data.build(dt.date(2020, 12, 31), db=MetaDB()),
                               refresh_sec=300)
    assert '<meta http-equiv="refresh" content="300">' in watched


def test_refresh_keeps_watch_interval(sandbox):
    """--watch 로 띄워둔 페이지를 daily 가 갱신 없는 파일로 덮으면 그 자리에서 멈춘다.

    사용자는 최신인 줄 알고 계속 본다. 그래서 기존 파일의 주기를 이어받는다.
    """
    dashboard.write(dt.date(2020, 12, 31), refresh_sec=120)
    dashboard.refresh_quietly(dt.date(2020, 12, 31))
    html = (sandbox / "reports" / "dashboard.html").read_text(encoding="utf-8")
    assert '<meta http-equiv="refresh" content="120">' in html


def test_refresh_quietly_never_raises(sandbox, monkeypatch):
    """대시보드는 보조 산출물이다. 여기서 죽어서 daily 가 멈추면 안 된다."""
    monkeypatch.setattr(dashboard, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert dashboard.refresh_quietly(dt.date(2020, 12, 31)) is None


def test_page_reports_its_own_age(sandbox):
    """자동 갱신되는 화면이 조용히 어제 숫자를 보여주는 것은 수동보다 나쁘다."""
    html = _html(sandbox)
    assert "generated_at" in html and "freshness" in html
    assert "quant report" in html          # 오래됐을 때 무엇을 해야 하는지 알려준다


def test_rankings_are_read_not_recomputed(sandbox, monkeypatch):
    """대시보드는 읽는 곳이다. 여기서 피처 패널을 다시 지으면 daily 가 그만큼 느려진다.

    실제로 그랬다 — daily 5.2초 중 1.6초가 대시보드의 패널 재계산이었다.
    """
    from app.features.builder import FeatureBuilder

    def boom(*a, **k):
        raise AssertionError("대시보드가 피처 패널을 다시 만들고 있습니다")

    monkeypatch.setattr(FeatureBuilder, "build", boom)
    monkeypatch.setattr(FeatureBuilder, "rankings", boom)
    data = dashboard_data.build(dt.date(2020, 12, 31), db=MetaDB())
    assert data["rankings"]["themes"] == []          # 파일이 없으면 빈 값, 예외 아님


def test_rankings_come_from_the_file_daily_wrote(sandbox):
    from app.paths import state_dir

    p = state_dir() / "rankings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "as_of": "2020-12-31",
        "themes": [{"theme": "uranium", "n": 2, "score": 84.2}],
        "countries": [], "screener": [],
    }), encoding="utf-8")
    r = dashboard_data.build(dt.date(2020, 12, 31), db=MetaDB())["rankings"]
    assert r["themes"] == [{"theme": "uranium", "n": 2.0, "score": 84.2}]
    assert r["as_of"] == "2020-12-31"


def test_w_pred_carries_b_pred_slot(sandbox):
    """#25 W_pred 는 B_pred 와 함께만 읽힌다. 자리를 비워두더라도 키는 항상 있다."""
    data = dashboard_data.build(dt.date(2020, 12, 31), db=MetaDB())
    for p in data["predictions"]:
        assert "b_pred" in p
