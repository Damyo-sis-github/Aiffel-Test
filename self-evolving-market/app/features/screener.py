"""§6.5 종목 스크리너 (3계층).

상위 3 테마 × 상위 3 국가 교집합 바스켓 내에서 상대강도·거래량·신고가 근접·유동성으로 랭킹.
출력은 **유니버스 편향 입력**이지 매수 지시가 아니다.
스크리너 파라미터도 진화 대상이며 같은 K 에 합산된다 (§10.4).
"""

from __future__ import annotations

import pandas as pd

from app.config import themes
from app.features.theme import _z, basket_members
from app.universe.snapshot import market_of, symbol_meta

# 국가 ETF → 그 국가의 종목이 사는 시장. KR/US 만 직접 종목을 보유한다.
COUNTRY_TO_MARKET = {"EWY": "KR", "SPY": "US", "QQQ": "US"}


def screen(
    snapshot: pd.DataFrame,
    theme_rank: pd.DataFrame,
    country_rank: pd.DataFrame,
) -> pd.DataFrame:
    cfg = (themes().get("scoring") or {}).get("screener") or {}
    n_theme = int(cfg.get("top_themes", 3))
    n_country = int(cfg.get("top_countries", 3))
    out_size = int(cfg.get("output_size", 20))

    if theme_rank.empty or snapshot.empty:
        return pd.DataFrame(columns=["symbol", "theme", "score"])

    top_themes = list(theme_rank.head(n_theme)["theme"])
    top_countries = list(country_rank.head(n_country)["symbol"]) if not country_rank.empty else []
    allowed_markets = {COUNTRY_TO_MARKET[c] for c in top_countries if c in COUNTRY_TO_MARKET}
    if not allowed_markets:
        allowed_markets = {"KR", "US"}   # 상위 국가가 직접 접근 불가 시장뿐이면 제약을 걸지 않는다

    members = basket_members()
    meta = symbol_meta()
    rows = []
    for theme in top_themes:
        for sym in members.get(theme, []):
            if sym not in snapshot.index:
                continue
            if meta.get(sym) and meta[sym].kind != "stock":
                continue           # 스크리너는 개별 종목만. ETF 는 1·2계층에서 이미 다뤘다.
            if market_of(sym) not in allowed_markets:
                continue
            r = snapshot.loc[sym]
            rows.append(
                {
                    "symbol": sym,
                    "market": market_of(sym),
                    "theme": theme,
                    "rs": r.get("rs_vs_bench", r.get("mom_63")),
                    "vol_growth": r.get("volr_5_60"),
                    "near_high": r.get("high_52w_gap"),
                    "liquidity": r.get("adv20"),
                }
            )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.drop_duplicates(subset=["symbol"], keep="first")
    df["score"] = (
        float(cfg.get("relative_strength", 35)) * _z(df["rs"])
        + float(cfg.get("volume_growth", 25)) * _z(df["vol_growth"])
        + float(cfg.get("near_high", 25)) * _z(df["near_high"])
        + float(cfg.get("liquidity", 15)) * _z(df["liquidity"])
    )
    return df.sort_values("score", ascending=False).head(out_size).reset_index(drop=True)
