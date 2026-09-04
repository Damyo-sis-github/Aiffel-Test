"""§6.1 매크로 피처. **release_date <= t 강제** (발표 전 지표를 쓰면 룩어헤드다)."""

from __future__ import annotations

import numpy as np
import pandas as pd

SERIES = {
    "DGS10": "y10",
    "DGS2": "y2",
    "DTWEXBGS": "dxy",
    "VIXCLS": "vix",
}


def build_macro_frame(macro: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """거래일 인덱스에 as-of 조인. 각 t 는 release_date <= t 인 최신 관측만 본다."""
    out = pd.DataFrame(index=pd.DatetimeIndex(dates, name="event_date"))
    if macro is None or macro.empty:
        for name in SERIES.values():
            out[name] = np.nan
        return _derive(out)

    for sid, name in SERIES.items():
        sub = macro[macro["series_id"] == sid]
        if sub.empty:
            out[name] = np.nan
            continue
        # 같은 release_date 에 여러 vintage 가 있으면 마지막 것을 쓴다.
        sub = (
            sub.sort_values(["release_date", "event_date"])
            .drop_duplicates(subset=["release_date"], keep="last")
            .loc[:, ["release_date", "value"]]
            .rename(columns={"release_date": "event_date", "value": name})
        )
        merged = pd.merge_asof(
            out.reset_index()[["event_date"]].sort_values("event_date"),
            sub.sort_values("event_date"),
            on="event_date",
            direction="backward",
        )
        out[name] = merged.set_index("event_date")[name].reindex(out.index).to_numpy()
    return _derive(out)


def _derive(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["curve_10_2"] = out.get("y10", np.nan) - out.get("y2", np.nan)
    out["curve_dir_20"] = out["curve_10_2"].diff(20)
    out["dxy_chg_20"] = out.get("dxy", pd.Series(np.nan, index=out.index)).pct_change(20)
    vix = out.get("vix", pd.Series(np.nan, index=out.index))
    out["vix_level"] = vix
    out["vix_high"] = (vix >= 20).astype(float)
    return out


def add_fx_features(macro_frame: pd.DataFrame, fx: pd.DataFrame, pair: str = "USDKRW") -> pd.DataFrame:
    """원/달러 20일 변화. §6.1"""
    out = macro_frame.copy()
    if fx is None or fx.empty:
        out["usdkrw_chg_20"] = np.nan
        return out
    sub = (
        fx[fx["pair"] == pair]
        .sort_values("event_date")
        .drop_duplicates(subset=["event_date"], keep="last")
        .loc[:, ["event_date", "rate"]]
    )
    merged = pd.merge_asof(
        out.reset_index()[["event_date"]].sort_values("event_date"),
        sub,
        on="event_date",
        direction="backward",
    ).set_index("event_date")
    # 환율 결측은 전일 이월 (§4.3)
    rate = merged["rate"].reindex(out.index).ffill()
    out["usdkrw"] = rate
    out["usdkrw_chg_20"] = rate.pct_change(20)
    return out


MACRO_COLUMNS = ("y10", "y2", "dxy", "vix", "curve_10_2", "curve_dir_20", "dxy_chg_20",
                 "vix_level", "vix_high", "usdkrw", "usdkrw_chg_20")
