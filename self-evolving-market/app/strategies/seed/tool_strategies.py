"""§7.2 SHORT_US / LEV_ETF / INV_ETF 시드 전략 + 벤치마크 + 대조군.

주의: 이 계열은 LONG 또는 ETF_ROT 에 active 전략이 있어야 채택될 수 있다 (§7.6, #28).
시드 백테스트는 전 계열 실행하되 채택 순서는 Curator 가 강제한다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.strategies.base import StrategyBase, pick, restrict_kinds


@dataclass
class InverseRegime(StrategyBase):
    """h5_inv_regime — 하락 레짐 + 지수 20일선 하향 이탈 시 인버스.

    근거: 하락 추세에서 지수 20일선 이탈은 며칠간 이어지는 경향이 있다.
    반증 조건: 하락 레짐 폴드에서 E <= 0. 횡보 레짐 폴드 E < -1%.
    엔진 하드 제약: 보유 <= 5 거래일, 하락 레짐에서만 진입.
    """

    id: str = "h5_inv_regime"
    family: str = "INV_ETF"
    horizon_days: int = 5
    params: dict = field(default_factory=lambda: {"top_n": 1, "ma_gap_max": 0.0})

    def _rank(self, feats: pd.DataFrame, date: dt.date) -> pd.DataFrame:
        f = restrict_kinds(feats, self.eligible_kinds())
        if f.empty or str(f["regime"].iloc[0]) != "bear":
            return pick(f.iloc[:0], pd.Series(dtype=float), +1, 0)
        # 지수 대용의 20일선 이탈 정도가 클수록 강한 신호
        sel = f[f["ma_gap_20"].notna()]
        # 인버스 ETF 자신의 20일선 상회 = 기초지수 하락 지속
        sel = sel[sel["ma_gap_20"] > float(self.params["ma_gap_max"])]
        return pick(sel, sel["ma_gap_20"], +1, int(self.params["top_n"]))


@dataclass
class LeverageTrend(StrategyBase):
    """h3_lev_trend — 강한 상승 레짐 + VIX<15 + 돌파 시 레버리지, 3일 청산.

    근거: 변동성이 낮은 상승 국면에서만 레버리지의 변동성 감쇠 비용이 추세 이익보다 작다.
    반증 조건: VIX<15 상승 레짐 폴드에서도 비용 차감 후 E <= 0.
    엔진 하드 제약: 보유 <= 5 거래일(전략 호라이즌 3), 상승 레짐에서만 진입.
    """

    id: str = "h3_lev_trend"
    family: str = "LEV_ETF"
    horizon_days: int = 3
    params: dict = field(default_factory=lambda: {"top_n": 1, "vix_max": 15.0})

    def _rank(self, feats: pd.DataFrame, date: dt.date) -> pd.DataFrame:
        f = restrict_kinds(feats, self.eligible_kinds())
        if f.empty or str(f["regime"].iloc[0]) != "bull":
            return pick(f.iloc[:0], pd.Series(dtype=float), +1, 0)
        vix = float(f["vix_level"].iloc[0]) if "vix_level" in f.columns else np.nan
        if vix == vix and vix >= float(self.params["vix_max"]):
            return pick(f.iloc[:0], pd.Series(dtype=float), +1, 0)
        sel = f[(f["breakout_20"] > 0) & (f["ma_gap_20"] > 0)]
        return pick(sel, sel["mom_21"], +1, int(self.params["top_n"]))


@dataclass
class ShortWeak(StrategyBase):
    """h10_short_weak — 상대강도 하위 + 200일선 하회 + 거래량 감소.

    근거: 관심에서 멀어진 약세 종목은 반등 촉매 없이 하락이 이어진다.
    반증 조건: 숏 진입 후 10일 수익이 대차비용 차감 후 음수인 폴드 40% 초과, 또는 숏스퀴즈로
              단일 거래 손실 15% 초과가 반복.
    엔진 하드 제약: 손절 -8% 강제, hard-to-borrow 제외, 시총 하위 20% 제외.
    """

    id: str = "h10_short_weak"
    family: str = "SHORT_US"
    horizon_days: int = 10
    params: dict = field(default_factory=lambda: {"top_n": 3, "max_volr": 0.9})

    def _rank(self, feats: pd.DataFrame, date: dt.date) -> pd.DataFrame:
        f = restrict_kinds(feats, self.eligible_kinds())
        f = f[f["market"] == "US"]                       # KR 개인 대주 공매도는 범위 밖 (§1.3)
        sel = f[(f["ma_gap_200"] < 0) & (f["volr_5_60"] <= float(self.params["max_volr"]))]
        # 상대강도가 낮을수록 높은 점수
        return pick(sel, -sel["rs_vs_bench"], -1, int(self.params["top_n"]))


@dataclass
class BuyAndHold(StrategyBase):
    """벤치마크. 매일 같은 심볼 1개를 보유한다 (엔진이 재진입을 무시한다)."""

    id: str = "bench_spy"
    family: str = "ETF_ROT"
    horizon_days: int = 252
    params: dict = field(default_factory=lambda: {"symbol": "SPY"})

    def _rank(self, feats: pd.DataFrame, date: dt.date) -> pd.DataFrame:
        sym = str(self.params["symbol"])
        if sym not in feats.index:
            return pick(feats.iloc[:0], pd.Series(dtype=float), +1, 0)
        return pd.DataFrame({"symbol": [sym], "score": [1.0], "side": [1]})


@dataclass
class RandomControl(StrategyBase):
    """random_ctrl — 무작위 대조군. **삭제 금지** (§7.2).

    게이트 5(부트스트랩 p<α_K)의 귀무분포를 만든다. 이게 없으면 다중검정 통제가 무너진다.
    ensemble_index 로 100개 앙상블을 만들고, 시드는 (전략시드, 앙상블번호, 날짜)로 고정한다.
    """

    id: str = "random_ctrl"
    family: str = "LONG"
    horizon_days: int = 5
    params: dict = field(default_factory=lambda: {"top_n": 5, "ensemble_index": 0})

    def _rank(self, feats: pd.DataFrame, date: dt.date) -> pd.DataFrame:
        f = restrict_kinds(feats, self.eligible_kinds())
        if f.empty:
            return pick(f, pd.Series(dtype=float), +1, 0)
        # PYTHONHASHSEED 에 흔들리지 않도록 산술 믹싱을 쓴다 (#11 멱등성).
        mix = (int(self.seed) * 1_000_003 + int(self.params.get("ensemble_index", 0))) * 2_654_435_761
        rng = np.random.default_rng((mix + date.toordinal()) % (2**32))
        scores = pd.Series(rng.random(len(f)), index=f.index)
        return pick(f, scores, +1, int(self.params["top_n"]))
