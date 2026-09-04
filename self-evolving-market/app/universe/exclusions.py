"""§5.5 / #26 이해충돌 종목 제외.

이 리스트에 든 종목은 유니버스에서 빠지고, 전략 신호 단계에서도 한 번 더 걸러진다.
이중으로 거르는 이유: 전략이 유니버스를 우회해 심볼을 하드코딩하는 경우를 막기 위해서다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

from app.config import exclusions


@dataclass(frozen=True)
class ExclusionList:
    symbols: frozenset[tuple[str, str]]      # (symbol, market)
    bare_symbols: frozenset[str]
    name_patterns: tuple[re.Pattern, ...]
    reasons: dict[str, str]

    def excludes(self, symbol: str, market: str | None = None, name: str = "") -> bool:
        if symbol in self.bare_symbols:
            return True
        if market and (symbol, market) in self.symbols:
            return True
        return any(p.search(name) for p in self.name_patterns) if name else False

    def filter(self, df: pd.DataFrame, symbol_col: str = "symbol", market_col: str = "market") -> pd.DataFrame:
        if df.empty or not (self.bare_symbols or self.name_patterns):
            return df
        markets = df[market_col] if market_col in df.columns else pd.Series("", index=df.index)
        mask = [
            not self.excludes(str(s), str(m) or None)
            for s, m in zip(df[symbol_col], markets, strict=True)
        ]
        return df.loc[mask].reset_index(drop=True)

    def __len__(self) -> int:
        return len(self.bare_symbols)


def load_exclusions() -> ExclusionList:
    cfg = exclusions()
    entries = cfg.get("symbols") or []
    pairs, bare, reasons = set(), set(), {}
    for e in entries:
        if isinstance(e, str):
            bare.add(e)
            continue
        sym = str(e.get("symbol", "")).strip()
        if not sym:
            continue
        bare.add(sym)
        pairs.add((sym, str(e.get("market", "")).strip()))
        reasons[sym] = str(e.get("reason", ""))
    patterns = tuple(re.compile(str(p)) for p in (cfg.get("name_patterns") or []))
    return ExclusionList(frozenset(pairs), frozenset(bare), patterns, reasons)
