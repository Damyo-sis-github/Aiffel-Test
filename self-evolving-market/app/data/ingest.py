"""인제스트 파이프라인: 소스 → 무결성 게이트 → PIT 저장.

as_of 규칙 (§4.2):
  prices.as_of = event_date   (그날 종가는 그날 장 마감에 알려진다)
  macro.as_of  = release_date (발표일)
  ingested_at  = 실제 기록 시각. PIT 필터에는 쓰지 않는다.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import pandas as pd

from app.config import runtime
from app.data.adapters.registry import get_macro_source, get_price_source
from app.data.integrity import Grade, IntegrityReport, run_price_gate
from app.data.pit_store import PITStore
from app.universe.snapshot import all_symbols, kinds_map

log = logging.getLogger(__name__)

MACRO_SERIES = ["DGS10", "DGS2", "DTWEXBGS", "VIXCLS"]
FX_PAIRS = {"USDKRW": "DEXKOUS"}


@dataclass
class IngestResult:
    market: str
    rows: int
    report: IntegrityReport

    @property
    def halted(self) -> bool:
        return self.report.halted


class Ingestor:
    def __init__(self, store: PITStore | None = None):
        self.store = store or PITStore()

    # ------------------------------------------------------------ 가격

    def ingest_prices(
        self,
        market: str,
        start: dt.date,
        end: dt.date,
        *,
        symbols: list[str] | None = None,
        ingested_at: dt.date | None = None,
    ) -> IngestResult:
        syms = symbols if symbols is not None else all_symbols(market=market)
        src = get_price_source(market)
        raw = src.fetch(syms, start, end)

        if raw.empty:
            rep = IntegrityReport(run_date=end, market=market)
            return IngestResult(market, 0, rep)

        stamped = raw.copy()
        stamped["market"] = market
        stamped["as_of"] = stamped["event_date"]          # ★ 가격의 as_of 는 event_date
        stamped["source"] = src.name
        stamped["ingested_at"] = pd.Timestamp(ingested_at or end)

        rep = run_price_gate(stamped, run_date=end, market=market, kinds=kinds_map())
        if rep.halted:
            log.error("무결성 게이트 중단(G3): %s", rep.summary())
            return IngestResult(market, 0, rep)

        # 격리 심볼은 저장하지 않고 당일 신호에서 뺀다.
        if quarantined := set(rep.quarantined):
            stamped = stamped[~stamped["symbol"].isin(quarantined)]

        n = self.store.write("prices", stamped)
        return IngestResult(market, n, rep)

    # ------------------------------------------------------------ 매크로·환율

    def ingest_macro(self, start: dt.date, end: dt.date, *, ingested_at: dt.date | None = None) -> int:
        src = get_macro_source()
        df = src.fetch(MACRO_SERIES, start, end)
        if df.empty:
            return 0
        df = df.copy()
        df["as_of"] = df["release_date"]                  # ★ 매크로의 as_of 는 release_date
        df["source"] = src.name
        df["ingested_at"] = pd.Timestamp(ingested_at or end)
        return self.store.write("macro", df)

    def ingest_fx(self, start: dt.date, end: dt.date, *, ingested_at: dt.date | None = None) -> int:
        src = get_macro_source()
        df = src.fetch(list(FX_PAIRS.values()), start, end)
        if df.empty:
            return 0
        inv = {v: k for k, v in FX_PAIRS.items()}
        out = pd.DataFrame(
            {
                "pair": df["series_id"].map(inv),
                "event_date": df["event_date"],
                "as_of": df["release_date"],
                # synthetic 매크로는 임의 레벨이므로 원/달러 스케일로 옮긴다.
                "rate": 1000.0 + df["value"] * 10.0 if src.name == "synthetic" else df["value"],
                "source": src.name,
                "ingested_at": pd.Timestamp(ingested_at or end),
            }
        )
        return self.store.write("fx", out.dropna(subset=["pair"]))

    # ------------------------------------------------------------ 백필

    def backfill(self, end: dt.date, *, start: dt.date | None = None) -> dict[str, int]:
        """§16 Phase 0: 2015~ 적재."""
        cfg = runtime().get("data_sources", {})
        start = start or dt.date.fromisoformat(str(cfg.get("history_start", "2015-01-01")))
        out: dict[str, int] = {}
        for market in ("KR", "US"):
            res = self.ingest_prices(market, start, end)
            out[f"prices_{market}"] = res.rows
            if res.halted:
                out["halted"] = 1
                return out
        out["macro"] = self.ingest_macro(start, end)
        out["fx"] = self.ingest_fx(start, end)
        return out


def integrity_rows(rep: IntegrityReport) -> list[dict]:
    """integrity_events 테이블 적재용."""
    ts = dt.datetime.now().isoformat(timespec="seconds")
    rows = []
    for f in rep.findings:
        rows.append(
            {
                "ts": ts,
                "run_date": rep.run_date.isoformat(),
                "check_name": f.check,
                "grade": f.grade.value,
                "symbol": ",".join(f.symbols[:50]) if f.symbols else None,
                "detail": f.detail,
            }
        )
    return rows


def grade_counts(rep: IntegrityReport) -> dict[str, int]:
    return {g.value: len(rep.by_grade(g)) for g in Grade}
