"""§13.1 일일 리포트 (학습 모드).

길이 상한: 텔레그램 요약 10줄, md 리포트 1페이지.
해석은 수치 뒤에, 수치보다 길지 않게.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.paths import reports_dir
from app.reports.glossary import GlossaryTracker, interpret

SYNTHETIC_BANNER = (
    "> ⚠️ **합성 데이터(SYNTHETIC)로 생성된 리포트입니다.** 실제 시장 결과가 아닙니다. "
    "실운영은 `config/runtime.yaml` 의 `data_sources.offline: false` 로 전환한 뒤입니다."
)
BYPASS_BANNER = "> ⚠️ **가드 우회(QUANT_GUARD_BYPASS=1) 상태에서 실행되었습니다.**"
SHORT_LABEL = "> ℹ️ 공매도 시뮬 가정: 리콜 없음·대차 항상 가능 → **실전 재현성 낮음**"


@dataclass
class DailyReport:
    date: dt.date
    mode: str                     # "실시간" | "보충"
    accounts: dict                # {ACC_S: {nav, cash, drawdown, exposures...}, ACC_L: {...}}
    trades_today: list[dict] = field(default_factory=list)
    performance: dict = field(default_factory=dict)     # {strategy_id: {n, win_rate, expectancy, ci}}
    prediction_stats: list = field(default_factory=list)
    exposures_by_family: dict = field(default_factory=dict)
    strategy_states: list[dict] = field(default_factory=list)
    integrity: dict = field(default_factory=dict)
    divergence: dict = field(default_factory=dict)
    killswitch: str = "정상"
    triggers: list[dict] = field(default_factory=list)
    synthetic: bool = False
    bypassed: bool = False
    has_short: bool = False
    notes: list[str] = field(default_factory=list)


def _pct(x, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "n/a"
    return f"{x * 100:.{digits}f}%"


def _num(x, digits: int = 0) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "n/a"
    return f"{x:,.{digits}f}"


def render_daily(rep: DailyReport, *, tracker: GlossaryTracker | None = None) -> tuple[str, str]:
    """(md 리포트, 텔레그램 10줄 요약)."""
    tracker = tracker or GlossaryTracker()
    L: list[str] = []
    add = L.append

    add(f"# 일일 리포트 — {rep.date} ({rep.mode})")
    if rep.synthetic:
        add(SYNTHETIC_BANNER)
    if rep.bypassed:
        add(BYPASS_BANNER)
    add("")

    # ---- 두 계좌 NAV
    add("## 두 계좌")
    add("| 계좌 | NAV | 현금 | 낙폭 | 명목 노출 | 숏 | 레버리지+인버스 |")
    add("|---|---:|---:|---:|---:|---:|---:|")
    for name in sorted(rep.accounts):
        a = rep.accounts[name]
        add(
            f"| {name} | {_num(a.get('nav'))} | {_num(a.get('cash'))} | {_pct(a.get('drawdown'))} "
            f"| {_pct(a.get('gross_notional_pct'))} | {_pct(a.get('short_notional_pct'))} "
            f"| {_pct(a.get('lev_inv_notional_pct'))} |"
        )
    if len(rep.accounts) == 2 and all("nav" in v for v in rep.accounts.values()):
        navs = {k: v["nav"] / v.get("capital", v["nav"] or 1) for k, v in rep.accounts.items()}
        gap = abs(list(navs.values())[0] - list(navs.values())[1])
        add("")
        add(f"- 두 계좌 수익률 괴리 {_pct(gap)} — 자본 크기 효과(정수 주식 제약). 크면 그게 정보다.")
    add("")

    # ---- 오늘 거래
    add("## 오늘 거래")
    if not rep.trades_today:
        add("- 없음")
    else:
        for t in rep.trades_today[:20]:
            add(
                f"- {t.get('account','')} {t.get('symbol')} {'매수' if int(t.get('side',1))>0 else '매도/숏'} "
                f"{_num(t.get('qty'), 2)}주 @ {_num(t.get('price'), 2)} ({t.get('reason','')})"
            )
    add("")

    # ---- 성과
    add("## 성과 (거래 승률 W_trade / 기대값 E)")
    if not rep.performance:
        add("- 청산 거래 없음")
    else:
        add("| 전략 | n | W_trade | 95% CI | E | 해석 |")
        add("|---|---:|---:|---|---:|---|")
        for sid in sorted(rep.performance):
            p = rep.performance[sid]
            n = int(p.get("n", 0))
            ci = p.get("ci") or (float("nan"), float("nan"))
            add(
                f"| {sid} | {n} | {_pct(p.get('win_rate'))} | [{_pct(ci[0])}, {_pct(ci[1])}] "
                f"| {_pct(p.get('expectancy'), 3)} | {interpret('win_rate', p.get('win_rate'), n=n)} |"
            )
        add("")
        add("- n < 30 은 참고용입니다. 공식 판정은 ACC_L 청산 100거래부터입니다 (§1.2).")
    add("")

    # ---- 예측 적중률
    add("## 예측 적중률 (W_pred vs 우연 기준선 B_pred)")
    if not rep.prediction_stats:
        add("- 아직 채점된 예측이 없습니다.")
    else:
        for s in rep.prediction_stats:
            add(f"- {s.line()} — {interpret('edge', s.edge)}")
    add("")

    # ---- 계열별 익스포저 · 전략 상태
    if rep.exposures_by_family:
        add("## 계열별 명목 익스포저")
        for fam in sorted(rep.exposures_by_family):
            add(f"- {fam}: {_pct(rep.exposures_by_family[fam])}")
        add("")
    if rep.strategy_states:
        add("## 활성 전략 상태")
        for s in rep.strategy_states:
            q = " (격리)" if s.get("quarantined") else ""
            add(f"- {s.get('strategy_id')} [{s.get('family')}] {s.get('status')}{q} — 배분 {_pct(s.get('allocation_pct'))}")
        add("")

    # ---- 데이터 게이트 · 괴리 · 킬스위치 · 트리거
    add("## 운영")
    add(f"- 데이터 게이트: {rep.integrity.get('summary', '결과 없음')}")
    if q := rep.integrity.get("quarantined"):
        add(f"  - 격리 심볼({len(q)}): {', '.join(list(q)[:10])}")
    add(f"- 백테스트 vs 시뮬 괴리: {rep.divergence.get('summary', '측정 없음')}")
    add(f"- 킬스위치: {rep.killswitch}")
    if rep.triggers:
        for t in rep.triggers:
            kind = "진화 트리거" if t.get("is_evolution") else "경고(진화 아님)"
            add(f"- {t.get('code')} [{kind}] {t.get('target')}: {t.get('reason')}")
    else:
        add("- 트리거: 없음 (진화 사이클 열리지 않음)")
    add(f"- 실행 구분: {rep.mode}")
    if rep.has_short:
        add("")
        add(SHORT_LABEL)
    for note in rep.notes:
        add(f"- {note}")

    body = "\n".join(L)

    # ---- 학습 모드: 새 용어 설명
    new_terms = tracker.new_terms(body, limit=2)
    if new_terms:
        body += "\n\n## 오늘의 용어\n" + "\n".join(f"- **{t}**: {d}" for t, d in new_terms)
        tracker.mark([t for t, _ in new_terms])

    return body, _telegram_summary(rep)


def _telegram_summary(rep: DailyReport, max_lines: int = 10) -> str:
    lines = [f"📊 {rep.date} 일일 요약 ({rep.mode})"]
    for name in sorted(rep.accounts):
        a = rep.accounts[name]
        lines.append(f"{name} NAV {_num(a.get('nav'))} / 낙폭 {_pct(a.get('drawdown'))}")
    if rep.performance:
        best = max(rep.performance.items(), key=lambda kv: kv[1].get("expectancy", -9e9))
        lines.append(f"최고 E: {best[0]} {_pct(best[1].get('expectancy'), 3)} (n={best[1].get('n', 0)})")
    if rep.prediction_stats:
        s = rep.prediction_stats[0]
        lines.append(f"W_pred h{s.horizon} {s.w_pred:.0%} vs B_pred {s.b_pred:.0%}")
    lines.append(f"거래 {len(rep.trades_today)}건 / 킬스위치 {rep.killswitch}")
    lines.append(f"데이터 게이트: {rep.integrity.get('summary', 'n/a')}")
    if rep.triggers:
        lines.append(f"트리거 {len(rep.triggers)}건: " + ", ".join(t.get("code", "") for t in rep.triggers))
    if rep.synthetic:
        lines.append("⚠️ 합성 데이터")
    return "\n".join(lines[:max_lines])


def write_daily(rep: DailyReport, body: str, out_dir: Path | None = None) -> Path:
    d = out_dir or (reports_dir() / "daily")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{rep.date.isoformat()}.md"
    p.write_text(body + "\n", encoding="utf-8")
    return p
