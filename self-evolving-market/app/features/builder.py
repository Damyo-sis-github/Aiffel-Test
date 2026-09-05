"""피처 패널 조립. 여기가 전략이 볼 수 있는 **유일한** 데이터 입구다.

PIT 보장
  - 가격은 PITStore.prices(as_of) 로만 읽는다.
  - 가격 피처는 조정가의 비율로만 계산한다 (app/features/price.py 주석 참조).
  - 매크로는 release_date 로 as-of 조인한다.
  - 따라서 `build(as_of=T)` 로 만든 패널을 t<=T 로 자른 결과는
    `build(as_of=t)` 결과와 같다. 이것을 tests/test_lookahead.py 가 셔플 테스트로 검증한다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.data.pit_store import PITStore
from app.features import macro as macro_feat
from app.features import price as price_feat
from app.features.country import score_countries
from app.features.regime import build_regime_frame
from app.features.relstrength import add_relative_strength
from app.features.screener import screen
from app.features.theme import score_themes
from app.universe.snapshot import all_symbols, symbol_meta

INDEX_PROXY = "SPY"


@dataclass
class FeaturePanel:
    """(event_date, symbol) 인덱스 피처 + 날짜 인덱스 매크로/레짐."""

    features: pd.DataFrame
    macro: pd.DataFrame
    regime: pd.DataFrame
    as_of: pd.Timestamp
    # 같은 패널을 여러 전략이 공유하므로 날짜별 스냅샷을 캐시한다(백테스트 핫 패스).
    _snap_cache: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def dates(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(sorted(self.features.index.get_level_values("event_date").unique()))

    def on(self, date: dt.date | pd.Timestamp) -> pd.DataFrame:
        """그 날짜의 종목별 피처 (symbol 인덱스). 없으면 빈 프레임."""
        ts = pd.Timestamp(date).normalize()
        if ts > self.as_of:
            raise ValueError(f"패널의 as_of({self.as_of.date()}) 이후 날짜는 조회할 수 없습니다: {ts.date()}")
        if (cached := self._snap_cache.get(ts)) is not None:
            return cached
        try:
            snap = self.features.xs(ts, level="event_date").copy()
        except KeyError:
            return pd.DataFrame(columns=self.features.columns)
        # 레짐·매크로는 그날의 스칼라다. 전략이 순수 함수로 남도록 컬럼으로 브로드캐스트한다.
        for k, v in self.regime_on(ts).items():
            snap[k] = v
        for k, v in self.macro_on(ts).items():
            snap[k] = v
        self._snap_cache[ts] = snap
        return snap

    def regime_on(self, date: dt.date | pd.Timestamp) -> dict[str, Any]:
        ts = pd.Timestamp(date).normalize()
        if ts not in self.regime.index:
            return {"regime": "sideways", "regime_code": "unknown", "regime_days": 0, "regime_confirmed": False}
        return self.regime.loc[ts].to_dict()

    def macro_on(self, date: dt.date | pd.Timestamp) -> dict[str, float]:
        ts = pd.Timestamp(date).normalize()
        if ts not in self.macro.index:
            return {}
        return {k: float(v) if v == v else np.nan for k, v in self.macro.loc[ts].items()}

    def slice_to(self, end: dt.date | pd.Timestamp) -> FeaturePanel:
        ts = pd.Timestamp(end).normalize()
        mask = self.features.index.get_level_values("event_date") <= ts
        return FeaturePanel(
            features=self.features.loc[mask],
            macro=self.macro.loc[self.macro.index <= ts],
            regime=self.regime.loc[self.regime.index <= ts],
            as_of=min(self.as_of, ts),
        )


class FeatureBuilder:
    def __init__(self, store: PITStore | None = None):
        self.store = store or PITStore()

    def build(
        self,
        as_of: dt.date,
        *,
        start: dt.date | None = None,
        symbols: list[str] | None = None,
        markets: tuple[str, ...] = ("US", "KR"),
    ) -> FeaturePanel:
        cutoff = pd.Timestamp(as_of).normalize()
        syms = symbols if symbols is not None else sorted(
            {s for m in markets for s in all_symbols(market=m)}
        )
        prices = self.store.prices(as_of, symbols=syms, start=start, adjusted=True)
        if prices.empty:
            empty = pd.DataFrame()
            return FeaturePanel(empty, pd.DataFrame(), pd.DataFrame(), cutoff)

        frames = [
            price_feat.compute(g)
            for _, g in prices.groupby("symbol", sort=True)
            if len(g) >= 2
        ]
        if not frames:
            # 데이터가 1일치뿐이면 여기서 pandas 가 "No objects to concatenate" 라는
            # 알아볼 수 없는 오류를 낸다. 원인을 말해주는 빈 패널로 대신한다.
            return FeaturePanel(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), cutoff)
        feats = pd.concat(frames, ignore_index=True)
        feats = (
            feats.set_index(["event_date", "symbol"])
            .sort_index()
            .astype({"market": "string"})
        )
        feats = add_relative_strength(feats, lookback=63)
        # 정적 메타(종목 종류·섹터·테마·레버리지)를 붙인다. 시점 불변이라 룩어헤드가 아니다.
        meta = symbol_meta()
        sym_level = feats.index.get_level_values("symbol")
        feats["kind"] = [meta[s].kind if s in meta else "stock" for s in sym_level]
        feats["sector"] = [meta[s].sector if s in meta else "" for s in sym_level]
        feats["theme"] = [meta[s].theme if s in meta else "" for s in sym_level]
        feats["leverage"] = [meta[s].leverage if s in meta else 1.0 for s in sym_level]

        dates = pd.DatetimeIndex(sorted(feats.index.get_level_values("event_date").unique()))
        macro_raw = self.store.macro(as_of, start=start)
        fx_raw = self.store.fx(as_of, start=start)
        macro_frame = macro_feat.build_macro_frame(macro_raw, dates)
        macro_frame = macro_feat.add_fx_features(macro_frame, fx_raw)

        index_gap = self._index_ma_gap(feats, dates)
        regime = build_regime_frame(index_gap, macro_frame)

        return FeaturePanel(features=feats, macro=macro_frame, regime=regime, as_of=cutoff)

    @staticmethod
    def _index_ma_gap(feats: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.Series:
        """지수 대용(SPY)의 200일선 괴리. 없으면 전체 종목 평균으로 대체한다."""
        try:
            s = feats.xs(INDEX_PROXY, level="symbol")["ma_gap_200"]
            return s.reindex(dates)
        except KeyError:
            return feats.groupby(level="event_date")["ma_gap_200"].mean().reindex(dates)

    # ------------------------------------------------------------ 3계층 랭킹

    def rankings(self, panel: FeaturePanel, date: dt.date) -> dict[str, pd.DataFrame]:
        """§6.3~6.5 테마·국가·종목 랭킹. daily/weekly 리포트와 컨텍스트 팩에 들어간다."""
        snap = panel.on(date)
        if snap.empty:
            empty = pd.DataFrame()
            return {"themes": empty, "countries": empty, "screener": empty}
        mac = panel.macro_on(date)
        # 원화 강세는 원/달러 하락이므로 부호를 뒤집는다.
        krw_strength = -float(mac.get("usdkrw_chg_20", 0.0) or 0.0)
        themes_df = score_themes(snap)
        countries_df = score_countries(snap, fx_strength={"EWY": krw_strength})
        screener_df = screen(snap, themes_df, countries_df)
        return {"themes": themes_df, "countries": countries_df, "screener": screener_df}
