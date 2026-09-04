"""§6.3 테마 (1계층). 바스켓 강도 = 상대강도 40 + 거래량 증가 20 + 폭 20 + 신고가 비율 20."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.config import themes


def _z(s: pd.Series) -> pd.Series:
    """순위 정규화 0~1. 이상치에 강하고 스케일 차이를 없앤다."""
    v = s.dropna()
    if len(v) <= 1:
        return pd.Series(0.5, index=s.index)
    return s.rank(pct=True, na_option="keep").fillna(0.5)


def basket_members(market: str | None = None) -> dict[str, list[str]]:
    cfg = themes().get("baskets") or {}
    out: dict[str, list[str]] = {}
    for theme, by_market in cfg.items():
        syms: list[str] = []
        for mk, lst in (by_market or {}).items():
            if market is None or mk == market:
                syms.extend(str(s) for s in (lst or []))
        if syms:
            out[theme] = sorted(set(syms))
    return out


def score_themes(snapshot: pd.DataFrame, *, market: str | None = None) -> pd.DataFrame:
    """snapshot: 특정 날짜의 종목별 피처 (symbol 인덱스).

    반환: theme, score, 구성요소 점수. 주간 랭킹에 쓴다.
    """
    w = (themes().get("scoring") or {}).get("theme") or {}
    lb = int(w.get("lookback_days", 63))
    rs_col = f"mom_{lb}" if f"mom_{lb}" in snapshot.columns else "mom_63"

    rows = []
    for theme, syms in basket_members(market).items():
        sub = snapshot.reindex([s for s in syms if s in snapshot.index]).dropna(how="all")
        if sub.empty:
            continue
        rs = float(sub["rs_vs_bench"].mean()) if "rs_vs_bench" in sub else float(sub[rs_col].mean())
        volg = float(sub["volr_5_60"].mean()) if "volr_5_60" in sub else np.nan
        breadth = float((sub[rs_col] > 0).mean())
        new_high = float((sub["high_52w_gap"] > -0.05).mean()) if "high_52w_gap" in sub else np.nan
        rows.append(
            {"theme": theme, "n": int(len(sub)), "rs": rs, "vol_growth": volg,
             "breadth": breadth, "new_high_ratio": new_high}
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["score"] = (
        float(w.get("relative_strength", 40)) * _z(df["rs"])
        + float(w.get("volume_growth", 20)) * _z(df["vol_growth"])
        + float(w.get("breadth", 20)) * _z(df["breadth"])
        + float(w.get("new_high_ratio", 20)) * _z(df["new_high_ratio"])
    )
    return df.sort_values("score", ascending=False).reset_index(drop=True)
