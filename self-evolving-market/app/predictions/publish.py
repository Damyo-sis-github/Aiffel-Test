"""§6.6 일일 예측 리스트 발행.

매일 h ∈ {1, 5, 20} 각각 Top-N 상승 + Bottom-N 하락 예측을 낸다.
점수 = 활성 전략 신호 가중합.

용도는 **조기 경보**다. 채택 게이트엔 쓰지 않는다 (게이트는 거래 승률만).
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from app.strategies.base import Strategy

HORIZONS = (1, 5, 20)
TOP_N = 10


def publish_predictions(
    snapshot: pd.DataFrame,
    date: dt.date,
    active: list[tuple[Strategy, float]],
    *,
    horizons: tuple[int, ...] = HORIZONS,
    top_n: int = TOP_N,
) -> pd.DataFrame:
    """active: (전략, 가중치) 목록. 반환 columns: pred_date, horizon, rank, symbol, market, side, score."""
    if snapshot is None or snapshot.empty or not active:
        return pd.DataFrame(
            columns=["pred_date", "horizon", "rank", "symbol", "market", "side", "score", "scored_at", "hit"]
        )

    rows = []
    for h in horizons:
        # 그 호라이즌에 가장 가까운 전략들만 쓴다 (h=1 예측에 h=60 전략을 섞지 않는다).
        weighted = [(s, w) for s, w in active if _horizon_bucket(int(s.horizon_days)) == h]
        if not weighted:
            weighted = active
        agg: dict[str, float] = {}
        for strat, weight in weighted:
            sig = strat.signals(snapshot, date)
            if sig.empty:
                continue
            # 전략 내부 점수 스케일이 제각각이므로 순위로 정규화한 뒤 합산한다.
            norm = sig["score"].rank(pct=True)
            for sym, sc, side in zip(sig["symbol"], norm, sig["side"], strict=True):
                agg[sym] = agg.get(sym, 0.0) + float(weight) * float(sc) * int(side)
        if not agg:
            continue
        ser = pd.Series(agg).sort_values(ascending=False)
        ups = ser[ser > 0].head(top_n)
        downs = ser[ser < 0].sort_values().head(top_n)
        for rank, (sym, sc) in enumerate(ups.items(), start=1):
            rows.append(_row(date, h, rank, sym, +1, sc, snapshot))
        for rank, (sym, sc) in enumerate(downs.items(), start=1):
            rows.append(_row(date, h, rank, sym, -1, sc, snapshot))
    return pd.DataFrame(rows)


def _horizon_bucket(h: int) -> int:
    if h <= 2:
        return 1
    if h <= 10:
        return 5
    return 20


def _row(date: dt.date, h: int, rank: int, symbol: str, side: int, score: float, snap: pd.DataFrame) -> dict:
    market = str(snap.loc[symbol, "market"]) if symbol in snap.index and "market" in snap.columns else ""
    return {
        "pred_date": date.isoformat(),
        "horizon": int(h),
        "rank": int(rank),
        "symbol": str(symbol),
        "market": market,
        "side": int(side),
        "score": float(score),
        "scored_at": None,
        "hit": None,
    }
