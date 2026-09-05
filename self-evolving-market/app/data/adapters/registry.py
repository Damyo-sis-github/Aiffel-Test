"""소스 선택. offline=true 이거나 실소스가 불가하면 synthetic 으로 폴백한다."""

from __future__ import annotations

import logging

from app.config import runtime
from app.data.adapters.base import MacroSource, PriceSource, SourceUnavailable
from app.data.adapters.synthetic import SyntheticMacroSource, SyntheticPriceSource

log = logging.getLogger(__name__)


def offline_mode() -> bool:
    return bool(runtime().get("data_sources", {}).get("offline", True))


def _synthetic_seed() -> int:
    return int(runtime().get("data_sources", {}).get("synthetic_seed", 20260903))


def get_price_source(market: str, *, allow_fallback: bool = True) -> PriceSource:
    cfg = runtime().get("data_sources", {})
    if offline_mode():
        return SyntheticPriceSource(market=market, seed=_synthetic_seed())

    want = str(cfg.get("kr_prices" if market == "KR" else "us_prices", "synthetic")).lower()
    try:
        if want == "synthetic":
            return SyntheticPriceSource(market=market, seed=_synthetic_seed())
        from app.data.adapters.market_sources import (
            FdrPriceSource,
            PykrxPriceSource,
            YFinancePriceSource,
        )

        if want == "yfinance":
            return YFinancePriceSource()
        if want == "pykrx":
            return PykrxPriceSource()
        if want == "fdr":
            return FdrPriceSource(market=market)
        raise SourceUnavailable(f"알 수 없는 가격 소스: {want}")
    except (SourceUnavailable, ImportError) as exc:
        if not allow_fallback:
            raise
        log.warning("가격 소스 '%s' 사용 불가 → synthetic 폴백: %s", want, exc)
        return SyntheticPriceSource(market=market, seed=_synthetic_seed())


def get_macro_source(*, allow_fallback: bool = True) -> MacroSource:
    cfg = runtime().get("data_sources", {})
    if offline_mode():
        return SyntheticMacroSource(seed=_synthetic_seed())
    want = str(cfg.get("macro", "synthetic")).lower()
    try:
        if want == "synthetic":
            return SyntheticMacroSource(seed=_synthetic_seed())
        from app.data.adapters.market_sources import FredMacroSource

        if want == "fred":
            return FredMacroSource()
        raise SourceUnavailable(f"알 수 없는 매크로 소스: {want}")
    except (SourceUnavailable, ImportError) as exc:
        if not allow_fallback:
            raise
        log.warning("매크로 소스 '%s' 사용 불가 → synthetic 폴백: %s", want, exc)
        return SyntheticMacroSource(seed=_synthetic_seed())


def active_source_labels() -> dict[str, str]:
    """리포트 배너용. synthetic 이 하나라도 있으면 리포트에 경고가 박힌다."""
    return {
        "KR": get_price_source("KR").name,
        "US": get_price_source("US").name,
        "macro": get_macro_source().name,
    }


def synthetic_in_use() -> bool:
    return "synthetic" in active_source_labels().values()
