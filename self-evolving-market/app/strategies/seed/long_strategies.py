"""§7.2 LONG / ETF_ROT 시드 전략.

전부 순수 함수. 손절·보유기간은 여기서 정하지 않는다(엔진 하드 제약).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

from app.strategies.base import StrategyBase, pick, restrict_kinds


@dataclass
class MeanRevRSI(StrategyBase):
    """h1_meanrev_rsi — RSI(2) 과매도 반등.

    근거: 단기 과잉반응. 유동성 있는 종목의 2일 RSI 극단값은 하루 뒤 평균회귀 경향이 있다.
    반증 조건: RSI(2)<10 진입의 1일 뒤 평균수익이 비용 차감 후 0 이하로 20일 이상 지속.
    """

    id: str = "h1_meanrev_rsi"
    family: str = "LONG"
    horizon_days: int = 1
    params: dict = field(default_factory=lambda: {"rsi_max": 10.0, "top_n": 5, "require_ma200": True})

    def _rank(self, feats: pd.DataFrame, date: dt.date) -> pd.DataFrame:
        f = restrict_kinds(feats, self.eligible_kinds())
        cond = f["rsi_2"] <= float(self.params["rsi_max"])
        if self.params.get("require_ma200", True):
            cond &= f["ma_gap_200"] > 0          # 추세 안에서의 눌림만
        sel = f[cond]
        # 더 과매도일수록 높은 점수
        return pick(sel, -sel["rsi_2"], +1, int(self.params["top_n"]))


@dataclass
class BreakoutVolume(StrategyBase):
    """h5_breakout_vol — 20일 고점 돌파 + 거래량 2배.

    근거: 정보 유입 직후 가격 반응은 즉시 완결되지 않고 며칠에 걸쳐 이어진다.
    반증 조건: 돌파 후 5일 수익이 비용 차감 후 음수인 폴드가 40% 초과.
    """

    id: str = "h5_breakout_vol"
    family: str = "LONG"
    horizon_days: int = 5
    params: dict = field(default_factory=lambda: {"vol_mult": 2.0, "top_n": 5})

    def _rank(self, feats: pd.DataFrame, date: dt.date) -> pd.DataFrame:
        f = restrict_kinds(feats, self.eligible_kinds())
        sel = f[(f["breakout_20"] > 0) & (f["volr_5_60"] >= float(self.params["vol_mult"]))]
        return pick(sel, sel["volr_5_60"] * (1 + sel["prior_high_20_gap"].fillna(0)), +1,
                    int(self.params["top_n"]))


@dataclass
class SectorRotation(StrategyBase):
    """h20_sector_rs — 섹터 ETF 상대강도 상위 3.

    근거: 섹터 자금 흐름은 분기 단위로 지속된다.
    반증 조건: 상위 3 섹터의 20일 초과수익이 하위 3 대비 유의하지 않게 되는 구간이 2폴드 연속.
    """

    id: str = "h20_sector_rs"
    family: str = "ETF_ROT"
    horizon_days: int = 20
    params: dict = field(default_factory=lambda: {"top_n": 3, "min_mom": 0.0})

    def _rank(self, feats: pd.DataFrame, date: dt.date) -> pd.DataFrame:
        f = feats[feats["kind"] == "etf"]
        sel = f[f["mom_63"] > float(self.params["min_mom"])]
        return pick(sel, sel["rs_vs_bench"], +1, int(self.params["top_n"]))


@dataclass
class CountryRotation(StrategyBase):
    """h20_country_rs — 국가 ETF 상대강도 상위 3.

    근거: 국가별 자금 유입은 통화·정책 사이클을 타고 수개월 지속된다.
    반증 조건: SPY 대비 상대강도 상위 국가의 20일 초과수익 평균이 0 이하로 6개월 지속.
    """

    id: str = "h20_country_rs"
    family: str = "ETF_ROT"
    horizon_days: int = 20
    params: dict = field(default_factory=lambda: {"top_n": 3, "exclude": ("SPY", "QQQ")})

    def _rank(self, feats: pd.DataFrame, date: dt.date) -> pd.DataFrame:
        f = feats[feats["kind"] == "country_etf"]
        f = f[~f.index.isin(self.params.get("exclude", ()))]
        sel = f[f["rs_vs_bench"] > 0]
        return pick(sel, sel["rs_vs_bench"], +1, int(self.params["top_n"]))


@dataclass
class MomentumMacro(StrategyBase):
    """h60_mom_macro — 12−1 모멘텀 + 금리차 필터.

    근거: 중기 모멘텀은 가장 오래 살아남은 이상현상이고, 금리차 역전 국면에서 무너진다.
    반증 조건: 금리차 확대 국면 폴드에서도 E <= 0.
    """

    id: str = "h60_mom_macro"
    family: str = "LONG"
    horizon_days: int = 60
    params: dict = field(default_factory=lambda: {"top_n": 8, "require_curve_up": False})

    def _rank(self, feats: pd.DataFrame, date: dt.date) -> pd.DataFrame:
        f = restrict_kinds(feats, self.eligible_kinds())
        if self.params.get("require_curve_up", False):
            curve = float(f["curve_dir_20"].iloc[0]) if "curve_dir_20" in f.columns and len(f) else 0.0
            if curve <= 0:
                return pick(f.iloc[:0], pd.Series(dtype=float), +1, 0)
        sel = f[(f["mom_12_1"] > 0) & (f["ma_gap_200"] > 0)]
        return pick(sel, sel["mom_12_1"], +1, int(self.params["top_n"]))
