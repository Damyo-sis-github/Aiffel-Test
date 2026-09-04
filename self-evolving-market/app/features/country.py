"""§6.4 국가·지역 (2계층).

점수 = SPY 대비 상대강도(63일) 40 + 현지 통화 강세 20 + 폭 20 + 거래량 증가 20.
KR/US 외 국가는 ETF 로만 접근한다 — 이 랭킹은 매수 지시가 아니라 유니버스 편향 입력이다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.config import etf_universe, themes
from app.features.theme import _z

BENCHMARK = "SPY"


def country_symbols() -> dict[str, str]:
    return {str(r["symbol"]): str(r.get("name", r["symbol"])) for r in (etf_universe().get("country_region") or [])}


def score_countries(
    snapshot: pd.DataFrame,
    *,
    fx_strength: dict[str, float] | None = None,
    breadth_by_country: dict[str, float] | None = None,
) -> pd.DataFrame:
    """snapshot: symbol 인덱스 피처. 국가 ETF 만 골라 점수화한다."""
    w = (themes().get("scoring") or {}).get("country") or {}
    lb = int(w.get("lookback_days", 63))
    col = f"mom_{lb}" if f"mom_{lb}" in snapshot.columns else "mom_63"
    fx_strength = fx_strength or {}
    breadth_by_country = breadth_by_country or {}

    names = country_symbols()
    present = [s for s in names if s in snapshot.index]
    if not present:
        return pd.DataFrame(columns=["symbol", "name", "score"])

    bench_mom = float(snapshot.loc[BENCHMARK, col]) if BENCHMARK in snapshot.index else 0.0
    rows = []
    for sym in present:
        r = snapshot.loc[sym]
        mom = float(r.get(col, np.nan))
        rows.append(
            {
                "symbol": sym,
                "name": names[sym],
                "rs_vs_spy": mom - bench_mom if mom == mom else np.nan,
                "currency_strength": float(fx_strength.get(sym, 0.0)),
                # 구성 상위 종목이 없으면 ETF 자신의 200일선 상회 여부로 폭을 대체한다.
                "breadth": float(breadth_by_country.get(sym, r.get("above_ma200", np.nan))),
                "vol_growth": float(r.get("volr_5_60", np.nan)),
            }
        )
    df = pd.DataFrame(rows)
    df["score"] = (
        float(w.get("relative_strength", 40)) * _z(df["rs_vs_spy"])
        + float(w.get("currency_strength", 20)) * _z(df["currency_strength"])
        + float(w.get("breadth", 20)) * _z(df["breadth"])
        + float(w.get("volume_growth", 20)) * _z(df["vol_growth"])
    )
    return df.sort_values("score", ascending=False).reset_index(drop=True)
