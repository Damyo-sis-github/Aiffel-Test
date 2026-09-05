"""대시보드가 그릴 데이터를 모은다. **HTML 을 만들지 않는다** — 순수 데이터라 테스트가 쉽다.

읽기 전용이다. 여기서 상태를 바꾸거나 게이트를 우회할 수 있는 경로는 없다.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import numpy as np
import pandas as pd

from app.alerts.router import AlertRouter
from app.data.adapters.registry import active_source_labels, synthetic_in_use
from app.data.meta_db import MetaDB
from app.evaluator.stats import wilson_ci
from app.evolve.trigger import read_pending
from app.guards.device import read_last as read_device_state
from app.portfolio.accounts import accounts, official_account
from app.portfolio.killswitch import KillSwitch

MAX_NAV_POINTS = 400
MAX_TABLE_ROWS = 60


def _f(x, default: float | None = None) -> float | None:
    """NaN·None 을 JSON 이 삼킬 수 있는 값으로."""
    if x is None:
        return default
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if np.isfinite(v) else default


def _nav_series(db: MetaDB) -> dict[str, Any]:
    """§8.1 두 계좌 NAV.

    ACC_S(100만)와 ACC_L(1억)은 자본이 100배 차이라 같은 축에 원화로 그릴 수 없다.
    **이중 축은 절대 쓰지 않는다** — 시작점 100 으로 지수화해 한 축에 올린다.
    """
    df = db.query("SELECT date, account, nav, drawdown FROM nav ORDER BY date")
    if df.empty:
        return {"dates": [], "series": [], "note": "NAV 기록이 없습니다."}

    wide = df.pivot(index="date", columns="account", values="nav").sort_index()
    if len(wide) > MAX_NAV_POINTS:                    # 긴 시계열은 균등 간격으로 솎는다
        wide = wide.iloc[:: max(1, len(wide) // MAX_NAV_POINTS)]

    caps = {n: a.capital_krw for n, a in accounts().items()}
    series = []
    for i, acc in enumerate(sorted(wide.columns)):
        base = caps.get(acc) or float(wide[acc].dropna().iloc[0])
        idx = (wide[acc] / base * 100.0).round(4)
        series.append({
            "name": acc,
            "slot": i + 1,                            # 카테고리 슬롯 순서 고정 (1=blue, 2=orange)
            "values": [_f(v) for v in idx],
            "official": acc == official_account(),
        })
    return {
        "dates": [str(d) for d in wide.index],
        "series": series,
        "unit": "시작 = 100",
        "note": "두 계좌의 자본이 100배 다르므로 시작점을 100으로 맞춰 한 축에 그립니다.",
    }


def _strategy_performance(db: MetaDB) -> list[dict]:
    """§1.2 전략별 W_trade / 기대값 E. 청산 거래만 센다 (#12)."""
    trades = db.query("SELECT * FROM trades WHERE closed = 1")
    states = db.query("SELECT strategy_id, family, status, allocation_pct, quarantined FROM strategy_state")
    state_map = {r["strategy_id"]: r for _, r in states.iterrows()} if not states.empty else {}

    out: list[dict] = []
    if not trades.empty:
        for sid, g in trades.groupby("strategy_id"):
            r = pd.to_numeric(g["pnl_pct"], errors="coerce").dropna()
            if r.empty:
                continue
            n = len(r)
            wins = int((r > 0).sum())
            lo, hi = wilson_ci(wins, n)
            st = state_map.get(sid, {})
            out.append({
                "strategy_id": sid,
                "family": str(g["family"].iloc[0] or st.get("family", "")),
                "status": str(st.get("status", "candidate")),
                "quarantined": bool(st.get("quarantined", 0)),
                "allocation_pct": _f(st.get("allocation_pct"), 0.0),
                "n": n,
                "win_rate": _f(wins / n),
                "ci_low": _f(lo), "ci_high": _f(hi),
                "expectancy": _f(r.mean()),
                "worst": _f(r.min()),
                "meaningful": n >= 30,               # #18 n<30 은 결론이 아니다
            })
    # 상태만 있고 아직 거래가 없는 전략도 표에는 보여준다
    for sid, r in state_map.items():
        if not any(o["strategy_id"] == sid for o in out):
            out.append({
                "strategy_id": sid, "family": str(r.get("family", "")),
                "status": str(r.get("status", "")), "quarantined": bool(r.get("quarantined", 0)),
                "allocation_pct": _f(r.get("allocation_pct"), 0.0), "n": 0,
                "win_rate": None, "ci_low": None, "ci_high": None,
                "expectancy": None, "worst": None, "meaningful": False,
            })
    return sorted(out, key=lambda x: (-(x["expectancy"] or -9e9), x["strategy_id"]))


def _prediction_stats(db: MetaDB) -> list[dict]:
    """§6.6 W_pred. **B_pred 없이 단독으로 내보내지 않는다** (#25).

    B_pred 는 계산 비용이 커서 daily 가 리포트에 남긴 값을 쓴다. 없으면 표시하지 않는다.
    """
    preds = db.query("SELECT horizon, side, hit FROM predictions WHERE hit IS NOT NULL")
    if preds.empty:
        return []
    out = []
    for h, g in preds.groupby("horizon"):
        hits = pd.to_numeric(g["hit"], errors="coerce").dropna()
        if hits.empty:
            continue
        n = len(hits)
        lo, hi = wilson_ci(int(hits.sum()), n)
        out.append({
            "horizon": int(h), "n": n,
            "w_pred": _f(hits.mean()), "ci_low": _f(lo), "ci_high": _f(hi),
            "b_pred": None,   # daily 리포트가 계산한 값이 있을 때만 채운다
        })
    return sorted(out, key=lambda x: x["horizon"])


def _rankings(as_of: dt.date) -> dict[str, list[dict]]:
    """§6.3~6.5 테마 → 국가 → 종목. PIT 로만 읽는다."""
    from app.data.pit_store import PITStore
    from app.features.builder import FeatureBuilder

    try:
        store = PITStore()
        panel = FeatureBuilder(store).build(as_of, start=as_of - dt.timedelta(days=400))
        r = FeatureBuilder(store).rankings(panel, as_of)
    except Exception:
        return {"themes": [], "countries": [], "screener": []}

    def rows(df: pd.DataFrame, keys: list[str], n: int = 8) -> list[dict]:
        if df is None or df.empty:
            return []
        keep = [k for k in keys if k in df.columns]
        return [
            {k: (_f(v) if isinstance(v, (int, float, np.number)) else str(v)) for k, v in row.items()}
            for row in df.head(n)[keep].to_dict("records")
        ]

    return {
        "themes": rows(r["themes"], ["theme", "n", "score"]),
        "countries": rows(r["countries"], ["symbol", "name", "score"]),
        "screener": rows(r["screener"], ["symbol", "market", "theme", "score"]),
    }


def _operations(db: MetaDB, as_of: dt.date) -> dict[str, Any]:
    """운영 상태. 상태색은 아이콘+라벨과 함께만 쓴다 (색 단독 금지)."""
    ks = KillSwitch().read()
    dev = read_device_state() or {}
    pending_trig = read_pending()
    router = AlertRouter(db)
    undelivered = router.pending()

    integ = db.query(
        "SELECT grade, COUNT(*) n FROM integrity_events WHERE run_date >= ? GROUP BY grade",
        ((as_of - dt.timedelta(days=30)).isoformat(),),
    )
    runs = db.query("SELECT run_date, task, mode, status, result_hash FROM run_log "
                    "ORDER BY run_date DESC LIMIT 10")
    return {
        "killswitch": {"tripped": bool(ks.tripped), "message": ks.message()},
        "device": {"level": dev.get("level"), "label": dev.get("level_label"),
                   "checked": dev.get("checked", False)},
        "integrity": {str(r["grade"]): int(r["n"]) for _, r in integ.iterrows()} if not integ.empty else {},
        "triggers": [t for t in (pending_trig.triggers if pending_trig else [])],
        "alerts_pending": int(len(undelivered)),
        "alerts_console_only": bool(router.console_only()),
        "runs": [{k: (None if pd.isna(v) else str(v)) for k, v in r.items()}
                 for r in runs.to_dict("records")] if not runs.empty else [],
    }


def build(as_of: dt.date | None = None, db: MetaDB | None = None) -> dict[str, Any]:
    """대시보드 전체 데이터. JSON 직렬화 가능해야 한다."""
    db = db or MetaDB()
    if as_of is None:
        last = db.scalar("SELECT MAX(run_date) FROM run_log WHERE task = 'daily'")
        as_of = dt.date.fromisoformat(str(last)) if last else dt.date.today()

    perf = _strategy_performance(db)
    closed_total = sum(p["n"] for p in perf)
    official = official_account()
    nav_row = db.query("SELECT nav, drawdown, gross_notional_pct, short_notional_pct, "
                       "lev_inv_notional_pct FROM nav WHERE account = ? ORDER BY date DESC LIMIT 1",
                       (official,))
    latest = nav_row.iloc[0].to_dict() if not nav_row.empty else {}

    all_r = pd.Series(dtype=float)
    trades = db.query("SELECT pnl_pct FROM trades WHERE closed = 1")
    if not trades.empty:
        all_r = pd.to_numeric(trades["pnl_pct"], errors="coerce").dropna()
    wins = int((all_r > 0).sum()) if len(all_r) else 0
    ci = wilson_ci(wins, len(all_r)) if len(all_r) else (None, None)

    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "as_of": as_of.isoformat(),
        "synthetic": bool(synthetic_in_use()),
        "sources": active_source_labels(),
        "official_account": official,
        "headline": {
            "nav": _f(latest.get("nav")),
            "capital": _f(accounts()[official].capital_krw),
            "drawdown": _f(latest.get("drawdown")),
            "gross_notional_pct": _f(latest.get("gross_notional_pct")),
            "short_notional_pct": _f(latest.get("short_notional_pct")),
            "lev_inv_notional_pct": _f(latest.get("lev_inv_notional_pct")),
            "closed_trades": closed_total,
            "official_threshold": 100,               # §1.2 공식 판정은 100 청산 거래부터
            "win_rate": _f(wins / len(all_r)) if len(all_r) else None,
            "win_ci": [_f(ci[0]), _f(ci[1])],
            "expectancy": _f(all_r.mean()) if len(all_r) else None,
        },
        "nav": _nav_series(db),
        "strategies": perf[:MAX_TABLE_ROWS],
        "predictions": _prediction_stats(db),
        "rankings": _rankings(as_of),
        "operations": _operations(db, as_of),
    }


def to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
