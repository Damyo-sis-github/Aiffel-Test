"""결정론적 합성 시장 데이터 — 오프라인/CI/데모 전용.

실운영에서는 절대 쓰지 않는다(`runtime.yaml: data_sources.offline: false`).
이 소스가 켜져 있으면 모든 리포트 상단에 SYNTHETIC 배너가 박힌다.

설계 요건
  1. **슬라이스 불변성**: [2015..2020] 을 뽑든 [2015..2025] 를 뽑든 겹치는 날의 값이 같아야 한다.
     구현: 항상 고정 지평(ANCHOR~HORIZON_END) 전체를 생성한 뒤 잘라낸다.
     그리고 난수 배열마다 **독립된 RNG** 를 쓴다 — 하나의 스트림에서 연달아 뽑으면
     앞선 배열의 길이가 뒤 배열의 오프셋을 바꿔 슬라이스 불변성이 깨진다.
     (이 버그는 실제로 한 번 났고, 같은 종목 가격이 조회 날짜마다 달라졌다.)
  2. **공통 시장 인자 + 레짐**: 상대강도·폭·레짐 피처가 의미를 갖도록 마켓 팩터를 공유한다.
  3. **OHLC 논리 보장**: low <= min(o,c), high >= max(o,c) — 무결성 게이트(§4.3)를 통과해야 한다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Sequence

import numpy as np
import pandas as pd

from app.data.adapters.base import PRICE_COLUMNS
from app.util.calendars import Market, trading_days

# 워밍업 앵커. history_start(2015-01-01) 보다 2년 앞서 시작해 252일 피처가 첫날부터 산다.
ANCHOR = dt.date(2013, 1, 2)
# 고정 지평. config/holidays_kr.yaml 이 커버하는 마지막 해까지.
HORIZON_END = dt.date(2027, 12, 31)


def _seed_for(*parts: object) -> int:
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") % (2**32 - 1)


def _rng(*parts: object) -> np.random.Generator:
    """용도마다 독립된 생성기. 스트림 공유를 하지 않는 것이 슬라이스 불변성의 핵심이다."""
    return np.random.default_rng(_seed_for(*parts))


class SyntheticPriceSource:
    """레짐이 전환되는 GBM. 마켓 팩터 1개 + 섹터 팩터 + 종목 고유 잡음."""

    name = "synthetic"

    def __init__(self, market: Market, seed: int = 20260903, anchor: dt.date = ANCHOR):
        self.market = market
        self.seed = int(seed)
        self.anchor = anchor
        self._cache: dict[str, np.ndarray] = {}
        self._days: list[dt.date] | None = None

    # ------------------------------------------------------------ 고정 지평

    def days(self) -> list[dt.date]:
        if self._days is None:
            self._days = trading_days(self.market, self.anchor, HORIZON_END)
        return self._days

    def _cached(self, key: str, fn) -> np.ndarray:
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    # ------------------------------------------------------------ 팩터

    def _market_factor(self) -> np.ndarray:
        return self._cached("mkt", self._market_factor_impl)

    def _market_factor_impl(self) -> np.ndarray:
        n = len(self.days())
        # 레짐 전환은 별도 스트림에서 뽑는다.
        switch = _rng(self.seed, "mkt_switch", self.market).random(n) < (1 / 70)
        picks = _rng(self.seed, "mkt_state", self.market).integers(0, 3, size=n)
        state = np.empty(n, dtype=int)
        cur = 0
        for i in range(n):
            if switch[i]:
                cur = int(picks[i])
            state[i] = cur
        mus = np.array([0.00055, 0.00005, -0.00075])
        vols = np.array([0.0075, 0.0095, 0.0175])
        z = _rng(self.seed, "mkt_shock", self.market).standard_normal(n)
        return mus[state] + vols[state] * z

    def _sector_factor(self, sector_key: str) -> np.ndarray:
        return self._cached(f"sec:{sector_key}", lambda: self._sector_factor_impl(sector_key))

    def _sector_factor_impl(self, sector_key: str) -> np.ndarray:
        n = len(self.days())
        slow = np.cumsum(_rng(self.seed, "sec_slow", self.market, sector_key).normal(0, 0.00035, n))
        slow = slow - pd.Series(slow).rolling(252, min_periods=1).mean().to_numpy()
        noise = _rng(self.seed, "sec_noise", self.market, sector_key).normal(0, 0.004, n)
        return np.diff(slow, prepend=0.0) + noise

    # ------------------------------------------------------------ 생성

    def _series(self, symbol: str) -> pd.DataFrame:
        return self._cached_frame(symbol)

    def _cached_frame(self, symbol: str) -> pd.DataFrame:
        key = f"frame:{symbol}"
        if key not in self._cache:
            self._cache[key] = self._build_series(symbol)  # type: ignore[assignment]
        return self._cache[key]  # type: ignore[return-value]

    def _build_series(self, symbol: str) -> pd.DataFrame:
        days = self.days()
        n = len(days)
        scalars = _rng(self.seed, "scalars", self.market, symbol)
        beta = float(scalars.uniform(0.6, 1.6))
        base = float(scalars.uniform(20, 400)) * (1000 if self.market == "KR" else 1)
        vol_base = float(scalars.uniform(3e5, 4e6))

        sector_key = hashlib.sha256(symbol.encode()).hexdigest()[:2]
        idio = _rng(self.seed, "idio", self.market, symbol).normal(0.0001, 0.011, n)
        ret = np.clip(beta * self._market_factor() + 0.6 * self._sector_factor(sector_key) + idio, -0.28, 0.28)
        close = base * np.exp(np.cumsum(ret))

        prev = np.concatenate([[close[0] / (1 + ret[0])], close[:-1]])
        gap = _rng(self.seed, "gap", self.market, symbol).normal(0, 0.004, n)
        open_ = np.clip(prev * (1 + gap), 1e-6, None)
        span = np.abs(_rng(self.seed, "span", self.market, symbol).normal(0, 0.008, n)) + 0.001
        # OHLC 논리 보장 (§4.3)
        high = np.maximum.reduce([np.maximum(open_, close) * (1 + span), open_, close])
        low = np.minimum.reduce([np.minimum(open_, close) * (1 - span), open_, close])

        vshock = _rng(self.seed, "vol", self.market, symbol).normal(0, 0.35, n)
        volume = np.round(vol_base * np.exp(vshock) * (1 + 3 * np.abs(ret)))

        if self.market == "KR":  # 정수 호가 근사
            open_, high, low, close = (np.round(x) for x in (open_, high, low, close))
            low = np.minimum.reduce([low, open_, close])
            high = np.maximum.reduce([high, open_, close])

        return pd.DataFrame(
            {
                "symbol": symbol,
                "event_date": pd.to_datetime(days),
                "open": open_, "high": high, "low": low, "close": close,
                "volume": volume, "adj_factor": 1.0,
            }
        )

    def fetch(self, symbols: Sequence[str], start: dt.date, end: dt.date) -> pd.DataFrame:
        if end < start or not self.days():
            return pd.DataFrame(columns=PRICE_COLUMNS)
        frames = [self._series(s) for s in sorted(set(symbols))]
        if not frames:
            return pd.DataFrame(columns=PRICE_COLUMNS)
        df = pd.concat(frames, ignore_index=True)
        mask = (df["event_date"] >= pd.Timestamp(start)) & (df["event_date"] <= pd.Timestamp(end))
        return df.loc[mask, PRICE_COLUMNS].reset_index(drop=True)


class SyntheticMacroSource:
    name = "synthetic"

    def __init__(self, seed: int = 20260903, anchor: dt.date = ANCHOR):
        self.seed = int(seed)
        self.anchor = anchor
        self._days: list[dt.date] | None = None

    def days(self) -> list[dt.date]:
        if self._days is None:
            self._days = trading_days("US", self.anchor, HORIZON_END)
        return self._days

    def fetch(self, series_ids: Sequence[str], start: dt.date, end: dt.date) -> pd.DataFrame:
        days = self.days()
        n = len(days)
        frames = []
        for sid in sorted(set(series_ids)):
            level = np.cumsum(_rng(self.seed, "macro", sid).normal(0, 0.02, n))
            level = level + float(_rng(self.seed, "macro_base", sid).uniform(1, 25))
            # 발표 지연: 지표별 0~30일 고정. release_date 가 as_of 가 된다 (§6.1).
            lag = int(_rng(self.seed, "macro_lag", sid).integers(0, 31))
            ev = pd.to_datetime(days)
            frames.append(
                pd.DataFrame(
                    {"series_id": sid, "event_date": ev,
                     "release_date": ev + pd.Timedelta(days=lag), "value": level}
                )
            )
        if not frames:
            return pd.DataFrame(columns=["series_id", "event_date", "release_date", "value"])
        df = pd.concat(frames, ignore_index=True)
        mask = (df["event_date"] >= pd.Timestamp(start)) & (df["event_date"] <= pd.Timestamp(end))
        return df.loc[mask].reset_index(drop=True)
