"""전략 레지스트리. 시드 + proposed(진화 산출물)를 한 곳에서 해석한다.

상태 전이 `proposed → candidate → active → retired` 는 Curator 가 관리하고
여기서는 "코드가 존재하는 전략" 만 다룬다. retired 코드·로그는 삭제하지 않는다 (§7.6).
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from functools import lru_cache

from app.strategies.base import Strategy, StrategyBase
from app.strategies.seed.long_strategies import (
    BreakoutVolume,
    CountryRotation,
    MeanRevRSI,
    MomentumMacro,
    SectorRotation,
)
from app.strategies.seed.tool_strategies import (
    BuyAndHold,
    InverseRegime,
    LeverageTrend,
    RandomControl,
    ShortWeak,
)

CONTROL_ID = "random_ctrl"
BENCHMARK_IDS = ("bench_spy", "bench_kospi")


def seed_strategies() -> list[Strategy]:
    """§7.2 시드 전략 전 계열."""
    return [
        MeanRevRSI(),
        BreakoutVolume(),
        SectorRotation(),
        CountryRotation(),
        MomentumMacro(),
        InverseRegime(),
        LeverageTrend(),
        ShortWeak(),
        BuyAndHold(id="bench_spy", params={"symbol": "SPY"}),
        BuyAndHold(id="bench_kospi", params={"symbol": "KS11"}),
        RandomControl(),
    ]


def control_ensemble(size: int, seed: int = 0) -> list[Strategy]:
    """게이트 5용 무작위 대조군 앙상블."""
    return [
        RandomControl(id=f"{CONTROL_ID}_{i:03d}", seed=seed, params={"top_n": 5, "ensemble_index": i})
        for i in range(int(size))
    ]


@lru_cache(maxsize=1)
def _proposed_strategies() -> tuple[Strategy, ...]:
    """app/strategies/proposed/ 에 있는 StrategyBase 하위 클래스를 자동 수집한다."""
    try:
        pkg = importlib.import_module("app.strategies.proposed")
    except ModuleNotFoundError:
        return ()
    out: list[Strategy] = []
    for mod in pkgutil.iter_modules(pkg.__path__):
        module = importlib.import_module(f"app.strategies.proposed.{mod.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, StrategyBase) and obj is not StrategyBase:
                try:
                    inst = obj()
                except TypeError:
                    continue
                if inst.id:
                    out.append(inst)
    return tuple(out)


def all_strategies(include_proposed: bool = True) -> list[Strategy]:
    out = list(seed_strategies())
    if include_proposed:
        seen = {s.id for s in out}
        out.extend(s for s in _proposed_strategies() if s.id not in seen)
    return out


def get_strategy(strategy_id: str) -> Strategy:
    for s in all_strategies():
        if s.id == strategy_id:
            return s
    if strategy_id.startswith(CONTROL_ID):
        idx = int(strategy_id.rsplit("_", 1)[-1]) if strategy_id != CONTROL_ID else 0
        return RandomControl(id=strategy_id, params={"top_n": 5, "ensemble_index": idx})
    raise KeyError(f"알 수 없는 전략: {strategy_id}")


def reset_cache() -> None:
    _proposed_strategies.cache_clear()
