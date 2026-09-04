"""§6.1 상대강도: 종목 vs 섹터, 섹터 vs 지수, 국가 ETF vs SPY."""

from __future__ import annotations

import pandas as pd

from app.universe.snapshot import symbol_meta

BENCH = {"US": "SPY", "KR": "122630"}  # KR 은 레버리지 ETF 밖에 지수 대용이 없어 SPY 로 US 만 사용
SECTOR_PROXY_COL = "sector_proxy"


def sector_proxy_map() -> dict[str, str]:
    """종목 → 그 종목의 섹터/테마 대표 ETF. 없으면 자기 자신."""
    meta = symbol_meta()
    theme_etf = {m.theme: s for s, m in meta.items() if m.kind == "etf" and m.theme}
    sector_to_theme = {
        "Semiconductor": "semiconductor",
        "Technology": "tech",
        "Energy": "energy",
        "Healthcare": "healthcare",
        "Financials": "financials",
        "Industrials": "industrials",
        "Defense": "defense",
        "CleanEnergy": "clean_energy",
        "Uranium": "uranium",
        "Space": "space",
        "IT": "tech",
        "2차전지": "battery",
        "헬스케어": "healthcare",
        "금융": "financials",
        "에너지": "energy",
        "산업재": "industrials",
        "방산": "defense",
        "신재생": "clean_energy",
    }
    out: dict[str, str] = {}
    for sym, m in meta.items():
        theme = m.theme or sector_to_theme.get(m.sector, "")
        proxy = theme_etf.get(theme)
        out[sym] = proxy if proxy and proxy != sym else ""
    return out


def add_relative_strength(panel: pd.DataFrame, lookback: int = 63) -> pd.DataFrame:
    """panel: (event_date, symbol) 인덱스 피처 패널. rs_* 컬럼을 붙여 돌려준다."""
    col = f"mom_{lookback}"
    if col not in panel.columns:
        raise KeyError(f"상대강도 계산에 {col} 이 필요합니다.")

    wide = panel[col].unstack("symbol")
    proxy = sector_proxy_map()
    out = panel.copy()

    # 종목 vs 섹터 ETF
    proxy_series = {}
    for sym in wide.columns:
        p = proxy.get(sym, "")
        proxy_series[sym] = wide[p] if p and p in wide.columns else pd.Series(0.0, index=wide.index)
    rs_sector = wide - pd.DataFrame(proxy_series, index=wide.index)
    out["rs_vs_sector"] = rs_sector.stack(future_stack=True).reindex(out.index)

    # 섹터/종목 vs 미국 지수
    bench = wide[BENCH["US"]] if BENCH["US"] in wide.columns else pd.Series(0.0, index=wide.index)
    rs_bench = wide.sub(bench, axis=0)
    out["rs_vs_bench"] = rs_bench.stack(future_stack=True).reindex(out.index)

    # 시장 내 순위 (0~1). 폭·스크리너에서 쓴다.
    out["rs_rank"] = (
        out.groupby(level="event_date")["rs_vs_bench"].rank(pct=True, na_option="keep")
    )
    return out
