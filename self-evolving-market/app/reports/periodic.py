"""§13.2 주간 / §13.3 월간 리포트."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

from app.config import gates as load_gates
from app.data.meta_db import MetaDB
from app.evaluator.stats import alpha_for_k, wilson_ci
from app.evolve.curator import Curator
from app.paths import reports_dir

OFFICIAL_TRADES = 100


def psi(expected: pd.Series, actual: pd.Series, bins: int = 10) -> float:
    """§13.2 피처 분포 이동 (PSI). > 0.2 면 경고."""
    e, a = expected.dropna(), actual.dropna()
    if len(e) < 20 or len(a) < 20:
        return float("nan")
    edges = np.unique(np.quantile(e, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return float("nan")
    pe = np.histogram(e, bins=edges)[0] / len(e)
    pa = np.histogram(a, bins=edges)[0] / len(a)
    pe, pa = np.clip(pe, 1e-6, None), np.clip(pa, 1e-6, None)
    return float(np.sum((pa - pe) * np.log(pa / pe)))


def _closed(db: MetaDB, account: str = "ACC_L") -> pd.DataFrame:
    df = db.query("SELECT * FROM trades WHERE closed = 1 AND account = ?", (account,))
    return df if df is not None else pd.DataFrame()


def write_weekly(date: dt.date, db: MetaDB | None = None) -> Path:
    db = db or MetaDB()
    closed = _closed(db)
    curator = Curator(db)
    L = [f"# 주간 리포트 — {date}", ""]

    L.append("## 전략별·계열별 성과")
    if closed.empty:
        L.append("- 청산 거래 없음")
    else:
        L.append("| 전략 | 계열 | n | W_trade | E |")
        L.append("|---|---|---:|---:|---:|")
        for (sid, fam), g in closed.groupby(["strategy_id", "family"], dropna=False):
            r = g["pnl_pct"].astype(float).dropna()
            if r.empty:
                continue
            L.append(f"| {sid} | {fam or '-'} | {len(r)} | {(r > 0).mean():.1%} | {r.mean():+.3%} |")
    L.append("")

    L.append("## 레짐 체류 · 랭킹 변동")
    regimes = db.query("SELECT date, account, nav FROM nav ORDER BY date DESC LIMIT 10")
    L.append(f"- 최근 NAV 기록 {len(regimes)}건")
    L.append("- 테마·국가·종목 랭킹은 daily 리포트의 랭킹 섹션을 주 단위로 비교하십시오.")
    L.append("")

    L.append("## ACC_S vs ACC_L 괴리 분해")
    navs = db.query("SELECT date, account, nav FROM nav ORDER BY date")
    if navs.empty:
        L.append("- NAV 기록 없음")
    else:
        wide = navs.pivot(index="date", columns="account", values="nav")
        if {"ACC_S", "ACC_L"} <= set(wide.columns):
            rs = wide["ACC_S"].pct_change().fillna(0).add(1).cumprod()
            rl = wide["ACC_L"].pct_change().fillna(0).add(1).cumprod()
            L.append(f"- 누적 수익률 괴리: {float(rs.iloc[-1] - rl.iloc[-1]):+.2%}")
            L.append("- 원인 후보: 정수 주식 제약, 종목 수 상한(5~8), 종목 한도 20% vs 5%.")
        else:
            L.append("- 두 계좌 NAV 가 모두 필요합니다.")
    L.append("")

    L.append("## 전략 상태")
    st = curator.states()
    L.extend(
        [f"- {r['strategy_id']} [{r['family']}] {r['status']} 배분 {float(r['allocation_pct']):.1%}"
         for _, r in st.iterrows()] or ["- 등록된 전략 상태 없음"]
    )
    return _write(date, "weekly", "\n".join(L))


def write_monthly(date: dt.date, db: MetaDB | None = None) -> Path:
    db = db or MetaDB()
    closed = _closed(db)
    k = db.current_k()
    base_alpha = float(load_gates()["common"]["control_base_alpha"])
    L = [f"# 월간 리포트 — {date:%Y-%m}", ""]

    L.append("## 공식 판정 상태 (ACC_L)")
    n = len(closed)
    L.append(f"- ACC_L 청산 거래 {n}건 / 공식 판정 기준 {OFFICIAL_TRADES}건")
    if n < OFFICIAL_TRADES:
        L.append(f"- **아직 공식 판정 불가.** {OFFICIAL_TRADES - n}건 더 필요합니다 (§13.3).")
    else:
        r = closed["pnl_pct"].astype(float).dropna()
        ci = wilson_ci(int((r > 0).sum()), len(r))
        L.append(f"- W_trade {float((r > 0).mean()):.1%} (95% CI [{ci[0]:.1%}, {ci[1]:.1%}]), E {float(r.mean()):+.3%}")
    L.append("")

    L.append("## 다중검정")
    L.append(f"- 누적 K = {k}, α_K = {alpha_for_k(base_alpha, max(k, 1)):.5f}")
    L.append("- K 는 리셋되지 않습니다. 스크리너 파라미터 탐색도 같은 K 에 합산됩니다 (§10.4).")
    L.append("")

    L.append("## 채택 당시 vs 재실행 (해시 재현)")
    runs = db.query("SELECT strategy_id, run_id, code_hash, data_hash, gates_hash, created_at "
                    "FROM strategy_runs ORDER BY created_at DESC LIMIT 20")
    if runs.empty:
        L.append("- 기록된 워크포워드 실행이 없습니다.")
    else:
        L.append("| 전략 | code | data | gates | 시각 |")
        L.append("|---|---|---|---|---|")
        for _, r in runs.iterrows():
            L.append(f"| {r['strategy_id']} | {r['code_hash']} | {r['data_hash']} | {r['gates_hash']} | {str(r['created_at'])[:16]} |")
        L.append("")
        L.append("- 재현 불가 시 채택은 무효입니다 (§13.4).")
    L.append("")

    L.append("## 실패 회고")
    if closed.empty:
        L.append("- 청산 거래 없음")
    else:
        by_reason = closed.groupby("exit_reason")["pnl_pct"].agg(["size", "mean"])
        for reason, row in by_reason.iterrows():
            L.append(f"- {reason}: {int(row['size'])}건, 평균 {float(row['mean']):+.3%}")
    L.append("")
    L.append("## 은퇴·전이 로그")
    ev = db.query("SELECT ts, action, reason FROM evolution_log ORDER BY seq DESC LIMIT 15")
    L.extend([f"- {str(r['ts'])[:16]} {r['action']}: {r['reason']}" for _, r in ev.iterrows()]
             or ["- 기록 없음"])
    return _write(date, "monthly", "\n".join(L))


def write_meta_review(date: dt.date, db: MetaDB | None = None) -> Path:
    """§16 6개월 시스템 메타 리뷰 — 전략이 아니라 **시스템**을 진화시킨다."""
    db = db or MetaDB()
    k = db.current_k()
    runs = db.query("SELECT strategy_id, metrics_json FROM strategy_runs")
    passed = 0
    for _, r in runs.iterrows():
        try:
            passed += int(json.loads(r["metrics_json"]).get("gates", {}).get("passed", False))
        except (json.JSONDecodeError, TypeError):
            continue
    integ = db.query("SELECT grade, COUNT(*) n FROM integrity_events GROUP BY grade")
    integ_lines = [f"- {r['grade']}: {int(r['n'])}건" for _, r in integ.iterrows()] or ["- 무결성 이벤트 없음"]
    L = [
        f"# 6개월 시스템 메타 리뷰 — {date}", "",
        "전략이 아니라 시스템을 점검한다. 결론은 사람이 낸다.", "",
        "## 데이터",
        *integ_lines,
        "- 질문: 격리·플래그 비율이 높은 소스는? 토스 vs API 괴리 추이는?", "",
        "## 게이트",
        f"- 총 시행 K = {k}, 게이트 통과 {passed}건 (통과율 {passed / max(len(runs), 1):.1%})",
        "- 질문: random_ctrl 통과율이 5% 근처인가? 게이트가 너무 엄격/느슨한가?", "",
        "## 가설",
        "- 질문: K 대비 채택률, 계열별 분포, 은퇴 사유 분포. 같은 실패를 반복하는가?", "",
        "## 비용 모델",
        "- 질문: 괴리 추적 결과로 슬리피지·수수료 가정을 재보정해야 하는가?", "",
        "## 유니버스",
        "- 질문: 테마·국가 랭킹 상위가 유니버스 밖에 있었던 적이 있는가?", "",
        "## 결론 (사람이 선택)",
        "- [ ] 계속",
        "- [ ] 게이트·유니버스·비용 모델 수정 후 계속",
        "- [ ] 목표 재정의",
        "- [ ] 중단",
        "",
        "어느 쪽이든 사유를 evolution_log 에 남긴다.",
    ]
    return _write(date, "meta_review", "\n".join(L))


def _write(date: dt.date, kind: str, body: str) -> Path:
    d = reports_dir() / kind
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{date.isoformat()}.md"
    p.write_text(body + "\n", encoding="utf-8")
    return p
