"""§6.2 레짐. Phase 1 은 규칙 기반 상태 (HMM 은 Phase 4).

상태 = 지수 vs 200일선 × VIX(<20 / >=20) × 금리차 방향  → 코드 문자열
라벨 = bull / sideways / bear  (LEV_ETF 는 bull, INV_ETF 는 bear 에서만 진입 가능, §7.6)
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

RegimeLabel = Literal["bull", "sideways", "bear"]
LABELS: tuple[str, ...] = ("bull", "sideways", "bear")


def classify(index_ma_gap: float, vix: float, curve_dir: float) -> tuple[str, str]:
    """(레짐 코드, 라벨). 결측은 sideways 로 보수적으로 처리한다."""
    if index_ma_gap is None or (isinstance(index_ma_gap, float) and np.isnan(index_ma_gap)):
        return "unknown", "sideways"
    trend = "above" if index_ma_gap > 0 else "below"
    vix_state = "lowvix" if (vix is not None and not np.isnan(vix) and vix < 20) else "highvix"
    if curve_dir is None or np.isnan(curve_dir):
        curve = "flat"
    else:
        curve = "steepening" if curve_dir > 0 else "flattening"
    code = f"{trend}|{vix_state}|{curve}"

    if trend == "above" and vix_state == "lowvix":
        label = "bull"
    elif trend == "below" and vix_state == "highvix":
        label = "bear"
    else:
        label = "sideways"
    return code, label


def build_regime_frame(index_ma_gap: pd.Series, macro_frame: pd.DataFrame) -> pd.DataFrame:
    """거래일별 레짐 시계열."""
    idx = macro_frame.index
    gap = index_ma_gap.reindex(idx)
    vix = macro_frame.get("vix_level", pd.Series(np.nan, index=idx)).reindex(idx)
    curve = macro_frame.get("curve_dir_20", pd.Series(np.nan, index=idx)).reindex(idx)

    codes, labels = [], []
    for g, v, c in zip(gap.to_numpy(), vix.to_numpy(), curve.to_numpy(), strict=True):
        code, label = classify(float(g) if g == g else np.nan, float(v) if v == v else np.nan,
                               float(c) if c == c else np.nan)
        codes.append(code)
        labels.append(label)

    out = pd.DataFrame({"regime_code": codes, "regime": labels}, index=idx)
    # T2 트리거: 상태 변경 후 5 거래일 유지 (§10.2)
    changed = out["regime"] != out["regime"].shift(1)
    grp = changed.cumsum()
    out["regime_days"] = out.groupby(grp).cumcount() + 1
    out["regime_confirmed"] = out["regime_days"] >= 5
    return out


def regime_matrix(trades: pd.DataFrame, regimes: pd.DataFrame) -> pd.DataFrame:
    """§6.2 레짐×전략×도구계열 성과 행렬."""
    if trades.empty:
        return pd.DataFrame(columns=["regime", "strategy_id", "family", "n", "expectancy", "win_rate"])
    t = trades.copy()
    t["signal_date"] = pd.to_datetime(t["signal_date"])
    reg = regimes["regime"].rename("regime")
    t = t.merge(reg, left_on="signal_date", right_index=True, how="left")
    t["regime"] = t["regime"].fillna("unknown")
    g = t.groupby(["regime", "strategy_id", "family"], dropna=False)["pnl_pct"]
    return (
        pd.DataFrame({"n": g.size(), "expectancy": g.mean(), "win_rate": g.apply(lambda s: float((s > 0).mean()))})
        .reset_index()
        .sort_values(["regime", "expectancy"], ascending=[True, False])
    )
