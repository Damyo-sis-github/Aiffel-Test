"""§7.4 워크포워드. 학습 36개월 → 검증 6개월, 6개월 스텝, 폴드 >= 10.

판정은 **검증창만** 본다. 학습창 결과는 IS/OOS 비교(게이트 7)에만 쓴다.
파라미터 그리드는 <= 50 이고 그리드 크기는 누적 K 에 합산된다 (§10.3-5).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from itertools import product
from typing import Any

import numpy as np
import pandas as pd

from app.backtest.costs import CostModel
from app.backtest.engine import BacktestEngine, BacktestResult, PriceBook, compute_metrics
from app.config import gates as load_gates
from app.features.builder import FeaturePanel
from app.strategies.base import Strategy


@dataclass
class Fold:
    index: int
    train_start: dt.date
    train_end: dt.date
    test_start: dt.date
    test_end: dt.date

    def label(self) -> str:
        return f"F{self.index:02d} train {self.train_start}~{self.train_end} test {self.test_start}~{self.test_end}"


@dataclass
class WalkForwardResult:
    strategy_id: str
    folds: list[Fold]
    test_results: list[BacktestResult]
    train_results: list[BacktestResult]
    combined_metrics: dict[str, Any]
    fold_expectancies: list[float] = field(default_factory=list)
    is_sharpe: float = float("nan")
    oos_sharpe: float = float("nan")
    grid_size: int = 1

    @property
    def all_test_trades(self) -> pd.DataFrame:
        frames = [r.trades for r in self.test_results if not r.trades.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    @property
    def all_test_nav(self) -> pd.DataFrame:
        frames = [r.nav for r in self.test_results if not r.nav.empty]
        return pd.concat(frames, ignore_index=True).sort_values("date") if frames else pd.DataFrame()


def make_folds(start: dt.date, end: dt.date, *, train_months: int, test_months: int, step_months: int) -> list[Fold]:
    folds: list[Fold] = []
    train_start = pd.Timestamp(start)
    i = 0
    while True:
        train_end = train_start + pd.DateOffset(months=train_months) - pd.Timedelta(days=1)
        test_start = train_end + pd.Timedelta(days=1)
        test_end = test_start + pd.DateOffset(months=test_months) - pd.Timedelta(days=1)
        if test_end.date() > end:
            break
        folds.append(
            Fold(i, train_start.date(), train_end.date(), test_start.date(), test_end.date())
        )
        train_start = train_start + pd.DateOffset(months=step_months)
        i += 1
    return folds


def expand_grid(grid: dict[str, list], max_size: int) -> list[dict]:
    """파라미터 그리드 전개. 상한을 넘으면 예외 (§7.4)."""
    if not grid:
        return [{}]
    keys = sorted(grid)
    combos = [dict(zip(keys, vals, strict=True)) for vals in product(*(grid[k] for k in keys))]
    if len(combos) > max_size:
        raise ValueError(f"파라미터 그리드 {len(combos)}개가 상한 {max_size}를 넘습니다 (§7.4).")
    return combos


class WalkForward:
    def __init__(
        self,
        panel: FeaturePanel,
        book: PriceBook,
        cost_model: CostModel,
        *,
        account: str = "ACC_L",
        gates_hash: str = "",
        data_hash: str = "",
    ):
        self.panel = panel
        self.book = book
        self.costs = cost_model
        self.account = account
        self.gates_hash = gates_hash
        self.data_hash = data_hash
        self.cfg = load_gates()["walkforward"]

    def run(
        self,
        strategy: Strategy,
        start: dt.date,
        end: dt.date,
        *,
        param_grid: dict[str, list] | None = None,
        require_min_folds: bool = True,
    ) -> WalkForwardResult:
        folds = make_folds(
            start, end,
            train_months=int(self.cfg["train_months"]),
            test_months=int(self.cfg["test_months"]),
            step_months=int(self.cfg["step_months"]),
        )
        if require_min_folds and len(folds) < int(self.cfg["min_folds"]):
            raise ValueError(
                f"폴드 {len(folds)}개는 최소 {self.cfg['min_folds']}개에 미달합니다. "
                f"기간을 늘리십시오 ({start}~{end})."
            )
        combos = expand_grid(param_grid or {}, int(self.cfg["max_param_grid"]))

        test_results: list[BacktestResult] = []
        train_results: list[BacktestResult] = []
        for fold in folds:
            # 학습창에서 파라미터를 고르고, **검증창에는 그 결과만** 적용한다.
            best_params, train_res = self._select_params(strategy, fold, combos)
            tuned = _with_params(strategy, best_params)
            engine = BacktestEngine(self.panel, self.book, self.costs, account=self.account)
            test_res = engine.run(
                tuned, fold.test_start, fold.test_end,
                gates_hash=self.gates_hash, data_hash=self.data_hash,
            )
            test_results.append(test_res)
            train_results.append(train_res)

        fold_e = [float(r.metrics.get("expectancy", np.nan)) for r in test_results]
        combined = _combine(test_results)
        is_sharpe = float(np.nanmean([r.metrics.get("sharpe", np.nan) for r in train_results])) if train_results else np.nan
        oos_sharpe = float(np.nanmean([r.metrics.get("sharpe", np.nan) for r in test_results])) if test_results else np.nan

        return WalkForwardResult(
            strategy_id=strategy.id, folds=folds, test_results=test_results, train_results=train_results,
            combined_metrics=combined, fold_expectancies=fold_e,
            is_sharpe=is_sharpe, oos_sharpe=oos_sharpe, grid_size=len(combos),
        )

    def _select_params(self, strategy: Strategy, fold: Fold, combos: list[dict]) -> tuple[dict, BacktestResult]:
        best, best_res, best_score = combos[0], None, -np.inf
        for params in combos:
            cand = _with_params(strategy, params)
            engine = BacktestEngine(self.panel, self.book, self.costs, account=self.account)
            res = engine.run(cand, fold.train_start, fold.train_end,
                             gates_hash=self.gates_hash, data_hash=self.data_hash)
            # 선택 기준은 기대값. 동률은 파라미터 사전순으로 깨서 결정론을 지킨다.
            score = float(res.metrics.get("expectancy", -np.inf))
            score = score if np.isfinite(score) else -np.inf
            if score > best_score:
                best, best_res, best_score = params, res, score
        return best, best_res


def _with_params(strategy: Strategy, params: dict):
    if not params:
        return strategy
    import copy

    tuned = copy.deepcopy(strategy)
    tuned.params = {**tuned.params, **params}
    return tuned


def _combine(results: list[BacktestResult]) -> dict:
    """검증 폴드 전체를 합쳐 하나의 지표로. NAV 는 폴드마다 자본이 리셋되므로
    수익률을 이어붙여(chain) 하나의 곡선으로 만든다."""
    trades = [r.trades for r in results if not r.trades.empty]
    tdf = pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()

    curves = []
    for r in results:
        if r.nav.empty:
            continue
        v = r.nav["nav"].to_numpy(float)
        curves.append(np.concatenate([[1.0], v[1:] / v[:-1]]) if len(v) > 1 else np.array([1.0]))
    if curves:
        chained = np.cumprod(np.concatenate(curves))
        peak = np.maximum.accumulate(chained)
        nav = pd.DataFrame(
            {"date": range(len(chained)), "nav": chained, "cash": 0.0, "drawdown": chained / peak - 1.0}
        )
        capital = 1.0
    else:
        nav, capital = pd.DataFrame(), 1.0
    return compute_metrics(tdf, nav, capital)
