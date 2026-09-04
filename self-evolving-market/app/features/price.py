"""§6.1 가격 피처.

**룩어헤드 없음의 근거**: 모든 피처는 조정가의 *비율*로만 계산한다.
미래의 배당·분할은 과거 가격 전체에 같은 상수를 곱하므로 두 과거 시점의 비율은 바뀌지 않는다.
따라서 오늘의 adj_factor 를 써도 모멘텀·변동성·MA 괴리·신고가 대비는 오염되지 않는다.
(레벨 자체를 쓰는 피처는 만들지 않는다.)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MOMENTUM_WINDOWS = (21, 63, 126, 252)
VOL_WINDOWS = (20, 60)
MA_WINDOWS = (20, 60, 200)


def _rsi(close: pd.Series, window: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def compute(df: pd.DataFrame) -> pd.DataFrame:
    """한 종목의 시계열(event_date 오름차순) → 피처 프레임.

    입력 컬럼: event_date, adj_close, adj_high, adj_low, adj_open, close, volume
    """
    d = df.sort_values("event_date").reset_index(drop=True)
    c, v = d["adj_close"], d["volume"]
    out = pd.DataFrame({"event_date": d["event_date"]})

    out["ret_1"] = c.pct_change()
    for w in MOMENTUM_WINDOWS:
        out[f"mom_{w}"] = c.pct_change(w)
    # 12-1 모멘텀: 최근 1개월을 뺀 12개월 (단기 반전 제거)
    out["mom_12_1"] = c.shift(21) / c.shift(252) - 1.0

    for w in VOL_WINDOWS:
        out[f"vol_{w}"] = out["ret_1"].rolling(w, min_periods=max(5, w // 2)).std() * np.sqrt(252)

    for w in MA_WINDOWS:
        ma = c.rolling(w, min_periods=max(5, w // 2)).mean()
        out[f"ma_gap_{w}"] = c / ma - 1.0
    out["above_ma200"] = (out["ma_gap_200"] > 0).astype(float)

    vol5 = v.rolling(5, min_periods=3).mean()
    vol60 = v.rolling(60, min_periods=20).mean()
    out["volr_5_60"] = vol5 / vol60.replace(0, np.nan)

    # 52주 신고가 대비 (오늘 포함). 값이 0 에 가까울수록 신고가 근접.
    high52 = c.rolling(252, min_periods=60).max()
    out["high_52w_gap"] = c / high52.replace(0, np.nan) - 1.0

    # 20일 고점 돌파: **전일까지의** 20일 고점을 넘었는가 (당일 고점 포함 금지)
    prior_high20 = c.shift(1).rolling(20, min_periods=10).max()
    out["breakout_20"] = (c > prior_high20).astype(float)
    out["prior_high_20_gap"] = c / prior_high20.replace(0, np.nan) - 1.0

    out["rsi_2"] = _rsi(c, 2)
    out["rsi_14"] = _rsi(c, 14)

    # 유동성: 20일 평균 거래대금 (원본 종가 기준 — 실제 체결 규모 판단용)
    out["adv20"] = (d["close"] * v).rolling(20, min_periods=10).mean()

    out["symbol"] = d["symbol"].to_numpy()
    out["market"] = d["market"].to_numpy()
    return out


FEATURE_COLUMNS: tuple[str, ...] = (
    "ret_1",
    *[f"mom_{w}" for w in MOMENTUM_WINDOWS],
    "mom_12_1",
    *[f"vol_{w}" for w in VOL_WINDOWS],
    *[f"ma_gap_{w}" for w in MA_WINDOWS],
    "above_ma200",
    "volr_5_60",
    "high_52w_gap",
    "breakout_20",
    "prior_high_20_gap",
    "rsi_2",
    "rsi_14",
    "adv20",
)
