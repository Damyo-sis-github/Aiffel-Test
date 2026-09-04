"""§6.6 예측 채점 → W_pred, 그리고 우연 기준선 B_pred.

B_pred = 같은 채점 기간에 **유니버스 전체**에서 (예측 방향대로) 움직인 종목 비율.
상승장에선 아무거나 찍어도 60% 맞는다. 그래서 W_pred 단독은 의미가 없고
W_pred − B_pred 로 판단한다. CI 는 일 단위 클러스터 부트스트랩으로 낸다 (#25).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.backtest.engine import PriceBook
from app.evaluator.stats import cluster_bootstrap_ci, effective_sample_size


@dataclass
class PredictionStats:
    horizon: int
    n: int
    w_pred: float
    b_pred: float
    edge: float                 # W_pred − B_pred
    edge_ci: tuple[float, float]
    effective_n: float

    def line(self) -> str:
        lo, hi = self.edge_ci
        return (
            f"h={self.horizon:2d}: W_pred {self.w_pred:.1%} vs B_pred {self.b_pred:.1%} "
            f"→ 차이 {self.edge:+.1%} (95% CI [{lo:+.1%}, {hi:+.1%}], n={self.n}, 유효 n≈{self.effective_n:.0f})"
        )

    def significant(self) -> bool:
        lo, hi = self.edge_ci
        return bool(np.isfinite(lo) and lo > 0)


def score_predictions(
    predictions: pd.DataFrame,
    book: PriceBook,
    as_of: dt.date,
) -> pd.DataFrame:
    """채점 만기가 지난 예측에 hit(0/1) 을 채운다. 이미 채점된 행은 건드리지 않는다(멱등)."""
    if predictions is None or predictions.empty:
        return predictions
    out = predictions.copy()
    pending = out["hit"].isna()
    for i in out.index[pending]:
        row = out.loc[i]
        pred_day = PriceBook.to_ord(dt.date.fromisoformat(str(row["pred_date"])))
        target = _nth_bar(book, str(row["symbol"]), pred_day, int(row["horizon"]))
        if target is None:
            continue
        target_day, px = target
        if target_day > PriceBook.to_ord(as_of):
            continue                      # 아직 만기 전
        base = book.last_close(str(row["symbol"]), pred_day)
        if base is None or base <= 0:
            continue
        ret = px / base - 1.0
        out.loc[i, "hit"] = int((ret > 0) == (int(row["side"]) > 0))
        out.loc[i, "scored_at"] = PriceBook.from_ord(target_day).isoformat()
    return out


def _nth_bar(book: PriceBook, symbol: str, from_ord: int, n: int) -> tuple[int, float] | None:
    cur = from_ord
    for _ in range(n):
        nxt = book.next_bar(symbol, cur)
        if nxt is None:
            return None
        cur = nxt[0]
    bar = book.bar(symbol, cur)
    return (cur, bar["c"]) if bar else None


def baseline(book: PriceBook, symbols: list[str], pred_days: list[int], horizon: int, side: int) -> float:
    """B_pred: 같은 기간·같은 방향으로 유니버스 전체가 맞았을 비율."""
    hits, total = 0, 0
    for day in pred_days:
        for sym in symbols:
            target = _nth_bar(book, sym, day, horizon)
            base = book.last_close(sym, day)
            if target is None or base is None or base <= 0:
                continue
            ret = target[1] / base - 1.0
            hits += int((ret > 0) == (side > 0))
            total += 1
    return hits / total if total else float("nan")


def summarize(
    scored: pd.DataFrame,
    book: PriceBook,
    universe: list[str],
    *,
    horizons: tuple[int, ...] = (1, 5, 20),
    max_baseline_days: int = 60,
) -> list[PredictionStats]:
    out: list[PredictionStats] = []
    if scored is None or scored.empty:
        return out
    done = scored[scored["hit"].notna()]
    for h in horizons:
        sub = done[done["horizon"] == h]
        if sub.empty:
            continue
        hits = sub["hit"].astype(float).to_numpy()
        clusters = sub["pred_date"].astype(str).to_numpy()
        w = float(hits.mean())

        days = sorted({PriceBook.to_ord(dt.date.fromisoformat(d)) for d in clusters})
        # B_pred 계산은 비싸다. 최근 N일 표본으로 근사하고 그 사실을 리포트에 적는다.
        sample_days = days[-max_baseline_days:]
        up = baseline(book, universe, sample_days, h, +1)
        down = 1.0 - up if np.isfinite(up) else float("nan")
        # 예측 방향 구성비로 가중한 기준선
        share_up = float((sub["side"] > 0).mean())
        b = share_up * up + (1 - share_up) * down if np.isfinite(up) else float("nan")

        edge_vals = hits - (b if np.isfinite(b) else 0.0)
        lo, hi = cluster_bootstrap_ci(edge_vals, clusters)
        out.append(
            PredictionStats(
                horizon=h, n=int(len(sub)), w_pred=w, b_pred=float(b),
                edge=float(w - b) if np.isfinite(b) else float("nan"),
                edge_ci=(lo, hi), effective_n=effective_sample_size(clusters),
            )
        )
    return out


def rolling_edge(scored: pd.DataFrame, window: int = 20) -> pd.Series:
    """T0 조기 경보용: W_pred − B_pred 의 20일 이동평균 (§10.2)."""
    if scored is None or scored.empty:
        return pd.Series(dtype=float)
    done = scored[scored["hit"].notna()].copy()
    if done.empty:
        return pd.Series(dtype=float)
    daily = done.groupby("pred_date")["hit"].mean()
    return daily.rolling(window, min_periods=max(5, window // 2)).mean()
