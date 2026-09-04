"""§5 유니버스 3계층 + 도구. 스냅샷은 매월 말 기록하고, 백테스트는 그 시점 스냅샷만 본다.

#2 생존편향: 스냅샷에는 그 시점에 상장돼 있던 종목이 들어가고, 이후 상폐된 종목도
남아 있어야 한다. "오늘 구성으로 과거를 돌리는" 코드는 이 모듈에 존재하지 않는다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from functools import lru_cache

import pandas as pd

from app.config import etf_universe, load_yaml
from app.data.pit_store import PITStore
from app.universe.exclusions import load_exclusions

STOCK_KINDS = ("stock",)
ETF_KINDS = ("etf", "country_etf", "lev_etf", "inv_etf")


@dataclass(frozen=True)
class SymbolMeta:
    symbol: str
    market: str
    kind: str
    sector: str = ""
    theme: str = ""
    leverage: float = 1.0
    name: str = ""
    delist_date: dt.date | None = None


def _seed() -> dict:
    return load_yaml("universe_seed.yaml")


@lru_cache(maxsize=1)
def symbol_meta() -> dict[str, SymbolMeta]:
    """심볼 → 메타. 종목·섹터/테마 ETF·국가 ETF·레버리지/인버스 ETF 전부."""
    out: dict[str, SymbolMeta] = {}
    seed, etf = _seed(), etf_universe()

    for market, rows in (seed.get("stocks") or {}).items():
        for r in rows:
            sym = str(r["symbol"])
            out[sym] = SymbolMeta(sym, market, "stock", str(r.get("sector", "")), name=str(r.get("name", "")))

    for market, rows in (etf.get("sector_theme") or {}).items():
        for r in rows:
            sym = str(r["symbol"])
            out[sym] = SymbolMeta(sym, market, "etf", theme=str(r.get("theme", "")), name=str(r.get("name", "")))

    for r in etf.get("country_region") or []:
        sym = str(r["symbol"])
        out[sym] = SymbolMeta(sym, "US", "country_etf", sector=str(r.get("region", "")), name=str(r.get("name", "")))

    for market, rows in (etf.get("leveraged_inverse") or {}).items():
        for r in rows:
            sym = str(r["symbol"])
            out[sym] = SymbolMeta(
                sym, market, str(r["kind"]), leverage=float(r["leverage"]), name=str(r.get("name", ""))
            )

    for r in seed.get("indices") or []:
        sym = str(r["symbol"])
        out[sym] = SymbolMeta(sym, str(r["market"]), "index", name=str(r.get("name", "")))

    for r in seed.get("simulated_delistings") or []:
        sym = str(r["symbol"])
        out[sym] = SymbolMeta(
            sym,
            str(r["market"]),
            "stock",
            str(r.get("sector", "")),
            delist_date=dt.date.fromisoformat(str(r["delist_date"])),
        )
    return out


def all_symbols(market: str | None = None, kinds: tuple[str, ...] | None = None) -> list[str]:
    meta = symbol_meta()
    return sorted(
        s
        for s, m in meta.items()
        if (market is None or m.market == market) and (kinds is None or m.kind in kinds)
    )


def leverage_of(symbol: str) -> float:
    m = symbol_meta().get(symbol)
    return float(m.leverage) if m else 1.0


def kind_of(symbol: str) -> str:
    m = symbol_meta().get(symbol)
    return m.kind if m else "stock"


def market_of(symbol: str) -> str:
    m = symbol_meta().get(symbol)
    return m.market if m else "US"


def kinds_map() -> dict[str, str]:
    return {s: m.kind for s, m in symbol_meta().items()}


class UniverseBuilder:
    """PIT 가격에서 adv20·mcap 근사를 계산해 스냅샷 행을 만든다."""

    def __init__(self, store: PITStore | None = None):
        self.store = store or PITStore()
        self.excl = load_exclusions()
        self.liquidity = _seed().get("liquidity") or {}

    def build(self, snapshot_date: dt.date, *, market: str | None = None) -> pd.DataFrame:
        meta = symbol_meta()
        targets = [m for m in meta.values() if market is None or m.market == market]
        if not targets:
            return pd.DataFrame()

        prices = self.store.prices(
            snapshot_date, symbols=[m.symbol for m in targets], lookback_days=40, adjusted=False
        )
        rows = []
        now = pd.Timestamp(snapshot_date)
        for m in targets:
            if self.excl.excludes(m.symbol, m.market, m.name):
                continue
            p = prices[prices["symbol"] == m.symbol]
            if p.empty:
                continue
            tail = p.sort_values("event_date").tail(20)
            adv20 = float((tail["close"] * tail["volume"]).mean())
            last_close = float(tail["close"].iloc[-1])
            delisted = m.delist_date is not None and m.delist_date <= snapshot_date
            rows.append(
                {
                    "snapshot_date": now,
                    "as_of": now,
                    "event_date": now,
                    "symbol": m.symbol,
                    "market": m.market,
                    "kind": m.kind,
                    "listed": not delisted,
                    "delist_date": pd.Timestamp(m.delist_date) if m.delist_date else pd.NaT,
                    # mcap 실데이터는 Phase 4. 지금은 거래대금 기반 근사이며 순위용으로만 쓴다.
                    "mcap": adv20 * 250.0,
                    "adv20": adv20,
                    "leverage": m.leverage,
                    "sector": m.sector,
                    "theme": m.theme,
                    "source": "derived",
                    "ingested_at": now,
                    "_last_close": last_close,
                }
            )
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        return self._apply_liquidity(df).drop(columns=["_last_close"])

    def _apply_liquidity(self, df: pd.DataFrame) -> pd.DataFrame:
        """§5.1 유동성 필터. ETF 는 필터 대상이 아니다(화이트리스트로 이미 통제)."""
        keep = []
        for _, r in df.iterrows():
            if r["kind"] != "stock":
                keep.append(True)
                continue
            floor = float((self.liquidity.get(r["market"], {}) or {}).get("adv20_min", 0))
            keep.append(bool(r["adv20"] >= floor))
        return df.loc[keep].reset_index(drop=True)

    def write(self, snapshot_date: dt.date, *, market: str | None = None) -> int:
        df = self.build(snapshot_date, market=market)
        return self.store.write("universe_snapshot", df) if not df.empty else 0

    @staticmethod
    def month_end_dates(start: dt.date, end: dt.date) -> list[dt.date]:
        """§5.6 매월 말 스냅샷 날짜."""
        idx = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="ME")
        return [d.date() for d in idx]
