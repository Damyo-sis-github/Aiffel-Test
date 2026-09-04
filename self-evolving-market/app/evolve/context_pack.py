"""§10.3-1 컨텍스트 팩.

Generator(/evolve 슬래시 커맨드)가 읽는 **유일한** 입력이다.
성과, 레짐×전략×계열 행렬, 테마·국가·종목 랭킹, W_pred 추이, 자동 실패 진단을 담는다.

주의: 이 팩에는 "게이트를 통과하는 방법"이 들어가면 안 된다. Generator 에게는
"왜 작동해야 하는가"를 요구한다 (§10.1).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.paths import state_dir

CONTEXT_FILE = "context_pack.json"

FAILURE_DIAGNOSES = {
    "cost": "비용: 총비용이 총이익의 절반을 넘습니다. 회전율이나 호라이즌을 의심하십시오.",
    "regime": "레짐: 특정 레짐 폴드에만 수익이 몰려 있습니다. 레짐 의존 전략일 수 있습니다.",
    "signal_decay": "신호 소멸: 최근 폴드로 갈수록 기대값이 단조 감소합니다.",
    "liquidity": "유동성: 거래대금 하위 종목에서 손실이 집중됩니다. 슬리피지 가정 재검토.",
    "decay": "변동성 감쇠: 레버리지·인버스 보유가 길수록 손실이 커집니다.",
    "short_squeeze": "숏스퀴즈: 단일 거래 최대 손실이 평균 손실의 3배를 넘습니다.",
}


def diagnose(trades: pd.DataFrame, fold_expectancies: list[float] | None = None) -> list[str]:
    """자동 실패 진단. 규칙 기반이며 LLM 이 아니라 코드가 낸다."""
    out: list[str] = []
    if trades is None or trades.empty:
        return ["거래 없음: 신호 조건이 너무 좁거나 유니버스와 맞지 않습니다."]
    closed = trades[trades["closed"] == 1]
    if closed.empty:
        return ["청산 거래 없음: 호라이즌·하드 제약을 확인하십시오."]

    gross = closed["gross_pnl"].abs().sum()
    cost = closed["cost"].sum() + closed["borrow_cost"].sum()
    if gross > 0 and cost / gross > 0.5:
        out.append(FAILURE_DIAGNOSES["cost"])

    if fold_expectancies and len(fold_expectancies) >= 3:
        arr = np.array([f for f in fold_expectancies if f == f])
        if arr.size >= 3 and np.all(np.diff(arr) < 0):
            out.append(FAILURE_DIAGNOSES["signal_decay"])

    if "instrument" in closed.columns:
        lev = closed[closed["instrument"].isin(["lev_etf", "inv_etf"])]
        if len(lev) >= 10 and lev["hold_days"].corr(lev["pnl_pct"]) < -0.2:
            out.append(FAILURE_DIAGNOSES["decay"])

    losses = closed.loc[closed["pnl_pct"] < 0, "pnl_pct"]
    if len(losses) >= 10 and losses.min() < 3 * losses.mean() and (closed["side"] < 0).any():
        out.append(FAILURE_DIAGNOSES["short_squeeze"])
    return out or ["뚜렷한 단일 원인이 잡히지 않습니다. 폴드별 성과를 직접 보십시오."]


def build_context_pack(
    *,
    today: dt.date,
    triggers: list[dict],
    performance: pd.DataFrame,
    regime_matrix: pd.DataFrame,
    rankings: dict[str, pd.DataFrame],
    prediction_stats: list[Any],
    strategy_states: pd.DataFrame,
    k_index: int,
    alpha_k: float,
    diagnoses: dict[str, list[str]],
    active_by_family: dict[str, int],
    open_families: dict[str, bool],
) -> dict:
    return {
        "spec_version": "0.5",
        "date": today.isoformat(),
        "triggers": triggers,
        "rules_reminder": [
            "가설마다 rationale(경제적 근거)과 falsifier(틀렸다고 볼 조건)가 없으면 Evaluator 로 가지 않는다.",
            "손절·리스크·익스포저 파라미터를 언급하거나 수정하는 가설은 자동 반려된다 (#27).",
            "evaluator/, config/gates.yaml, config/risk.yaml 은 수정할 수 없다 (G2).",
            "SHORT_US/LEV_ETF/INV_ETF 는 LONG 또는 ETF_ROT 에 active 전략이 있을 때만 열린다 (#28).",
            "사이클당 가설 <= 5, 같은 계열 <= 2, 유사도 > 0.9 는 반려.",
            "요구하는 것은 '게이트를 통과하는 방법'이 아니라 '왜 작동해야 하는가'다.",
        ],
        "multiple_testing": {"k_index": k_index, "alpha_k": alpha_k,
                             "note": "K 는 리셋되지 않는다. 스크리너 파라미터도 같은 K 에 합산된다."},
        "family_gate": {"active_by_family": active_by_family, "open": open_families},
        "performance": _records(performance),
        "regime_matrix": _records(regime_matrix),
        "rankings": {k: _records(v.head(10)) for k, v in rankings.items()},
        "prediction_stats": [
            {"horizon": s.horizon, "n": s.n, "w_pred": s.w_pred, "b_pred": s.b_pred,
             "edge": s.edge, "edge_ci": list(s.edge_ci), "significant": s.significant()}
            for s in (prediction_stats or [])
        ],
        "strategy_states": _records(strategy_states),
        "failure_diagnoses": diagnoses,
    }


def _records(df: pd.DataFrame | None) -> list[dict]:
    if df is None or df.empty:
        return []
    return json.loads(df.to_json(orient="records", date_format="iso"))


def write_context_pack(pack: dict, path: Path | None = None) -> Path:
    p = path or (state_dir() / CONTEXT_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(pack, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p
