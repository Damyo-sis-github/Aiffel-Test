"""#3 비용 · #4 과적합 · #5 다중검정 · #12 승률 정의 · #25 예측 착시."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from app.backtest.costs import CostModel
from app.evaluator.gates import evaluate
from app.evaluator.stats import (
    alpha_for_k,
    bootstrap_p_value,
    cluster_bootstrap_ci,
    wilson_ci,
)


# ---------------------------------------------------------------- #3 비용
def test_cost_model_is_required(seeded):
    from app.backtest.engine import BacktestEngine

    with pytest.raises(ValueError, match="필수 인자"):
        BacktestEngine(panel=None, book=None, cost_model=None)


def test_costs_config_missing_key_raises(sandbox):
    import yaml

    from app.config import ConfigError, _load_yaml_cached, costs
    from app.paths import config_dir

    p = config_dir() / "costs.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    del data["markets"]["KR"]["slippage_rate"]
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    _load_yaml_cached.cache_clear()
    with pytest.raises(ConfigError, match="기본값은 허용되지 않습니다"):
        costs()


def test_zero_cost_model_is_explicit(seeded):
    real, zero = CostModel.from_config(), CostModel.zero()
    assert real.enabled and not zero.enabled
    assert zero.slippage_rate("KR", "stock") == 0.0
    assert real.slippage_rate("KR", "stock") > 0.0


def test_sell_side_tax_applies_only_on_sell(seeded):
    cm = CostModel.from_config()
    buy = cm.fill(ref_price=100, qty=10, side=+1, opening=True, market="KR", kind="stock", is_short=False)
    sell = cm.fill(ref_price=100, qty=10, side=-1, opening=False, market="KR", kind="stock", is_short=False)
    assert sell.cost > buy.cost, "KR 매도 거래세가 반영되어야 한다"


def test_short_entry_slippage_multiplier(seeded):
    cm = CostModel.from_config()
    long_fill = cm.fill(ref_price=100, qty=1, side=-1, opening=True, market="US", kind="stock", is_short=False)
    short_fill = cm.fill(ref_price=100, qty=1, side=-1, opening=True, market="US", kind="stock", is_short=True)
    assert short_fill.slippage_rate > long_fill.slippage_rate


def test_liquidity_multiplier(seeded):
    cm = CostModel.from_config()
    small = cm.fill(ref_price=100, qty=1, side=1, opening=True, market="US", kind="stock",
                    is_short=False, adv20=1e9)
    large = cm.fill(ref_price=100, qty=1e6, side=1, opening=True, market="US", kind="stock",
                    is_short=False, adv20=1e6)
    assert large.slippage_rate > small.slippage_rate


def test_leveraged_etf_carries_expense_ratio(seeded):
    cm = CostModel.from_config()
    assert cm.carry(notional=1_000_000, kind="lev_etf", is_short=False) > 0
    assert cm.carry(notional=1_000_000, kind="stock", is_short=False) == 0
    assert cm.carry(notional=1_000_000, kind="stock", is_short=True) > 0   # 대차수수료


# ---------------------------------------------------------------- #5 다중검정
def test_alpha_shrinks_with_k():
    assert alpha_for_k(0.05, 1) == pytest.approx(0.05)
    assert alpha_for_k(0.05, 4) == pytest.approx(0.025)
    assert alpha_for_k(0.05, 100) == pytest.approx(0.005)
    assert alpha_for_k(0.05, 400) < alpha_for_k(0.05, 100)


def test_k_index_never_resets(seeded):
    from app.data.meta_db import MetaDB

    db = MetaDB()
    assert db.current_k() == 0
    for i in range(3):
        k = db.next_k_index()
        db.upsert("multiple_testing", [{"k_index": k, "cycle_id": "c", "strategy_id": f"s{i}",
                                        "created_at": "2020-01-01"}])
    assert db.current_k() == 3
    assert db.next_k_index() == 4


def test_bootstrap_p_value_behaviour():
    ctrl = list(np.random.default_rng(0).normal(0, 0.001, 100))
    assert bootstrap_p_value(0.05, ctrl) < 0.01          # 대조군을 크게 상회
    assert bootstrap_p_value(-0.05, ctrl) > 0.9          # 크게 하회
    assert bootstrap_p_value(0.05, []) == 1.0            # 대조군이 없으면 통과 불가


def test_bootstrap_is_deterministic():
    ctrl = [0.001 * i for i in range(50)]
    assert bootstrap_p_value(0.02, ctrl) == bootstrap_p_value(0.02, ctrl)


def test_random_control_cannot_pass_gate_easily(seeded):
    """대조군 대비 우연 수준 성과는 게이트 5를 통과하면 안 된다."""
    ctrl = list(np.random.default_rng(1).normal(0.001, 0.002, 100))
    metrics = {"n_closed": 100, "win_rate": 0.52, "expectancy": float(np.mean(ctrl)),
               "sharpe": 0.6, "mdd": 0.1, "worst_trade": -0.05}
    rep = evaluate(metrics, strategy_id="ctrl", family="LONG", horizon_days=5, k_index=1,
                   control_expectancies=ctrl, fold_expectancies=[0.001] * 10,
                   is_sharpe=0.7, oos_sharpe=0.6)
    g5 = next(r for r in rep.results if r.number == "5")
    assert not g5.passed


# ---------------------------------------------------------------- 게이트 일반
def _good_metrics() -> dict:
    return {"n_closed": 120, "win_rate": 0.56, "expectancy": 0.012, "sharpe": 1.1,
            "mdd": 0.11, "worst_trade": -0.07}


def test_gate_passes_when_everything_good(seeded):
    ctrl = list(np.random.default_rng(2).normal(0.0, 0.001, 100))
    rep = evaluate(_good_metrics(), strategy_id="good", family="LONG", horizon_days=5, k_index=1,
                   control_expectancies=ctrl, fold_expectancies=[0.01] * 10,
                   is_sharpe=1.3, oos_sharpe=1.1)
    assert rep.passed, rep.summary()


def test_gate_fails_on_negative_expectancy(seeded):
    m = _good_metrics() | {"expectancy": -0.001}
    rep = evaluate(m, strategy_id="bad", family="LONG", horizon_days=5, k_index=1,
                   control_expectancies=[0.0] * 100, fold_expectancies=[0.01] * 10,
                   is_sharpe=1.0, oos_sharpe=1.0)
    assert not rep.passed
    assert any(r.number == "1" and not r.passed for r in rep.results)


def test_gate_fails_on_small_sample(seeded):
    m = _good_metrics() | {"n_closed": 12}
    rep = evaluate(m, strategy_id="small", family="LONG", horizon_days=5, k_index=1,
                   control_expectancies=[0.0] * 100, fold_expectancies=[0.01] * 10,
                   is_sharpe=1.0, oos_sharpe=1.0)
    fails = {r.number for r in rep.failures}
    assert {"2", "8"} <= fails


def test_gate7_fails_when_is_oos_unavailable(seeded):
    """IS/OOS 를 못 계산하면 통과시키지 않는다 (보수적)."""
    rep = evaluate(_good_metrics(), strategy_id="x", family="LONG", horizon_days=5, k_index=1,
                   control_expectancies=[0.0] * 100, fold_expectancies=[0.01] * 10,
                   is_sharpe=None, oos_sharpe=None)
    assert any(r.number == "7" and not r.passed for r in rep.results)


def test_gate7_detects_overfitting(seeded):
    rep = evaluate(_good_metrics(), strategy_id="of", family="LONG", horizon_days=5, k_index=1,
                   control_expectancies=[0.0] * 100, fold_expectancies=[0.01] * 10,
                   is_sharpe=3.0, oos_sharpe=1.0)          # 비율 3.0 > 2.0
    assert any(r.number == "7" and not r.passed for r in rep.results)


def test_short_horizon_needs_higher_sharpe(seeded):
    m = _good_metrics() | {"sharpe": 0.6}
    ok = evaluate(m, strategy_id="h5", family="LONG", horizon_days=5, k_index=1,
                  control_expectancies=[0.0] * 100, fold_expectancies=[0.01] * 10,
                  is_sharpe=0.7, oos_sharpe=0.6)
    ng = evaluate(m, strategy_id="h1", family="LONG", horizon_days=1, k_index=1,
                  control_expectancies=[0.0] * 100, fold_expectancies=[0.01] * 10,
                  is_sharpe=0.7, oos_sharpe=0.6)
    assert next(r for r in ok.results if r.number == "3").passed
    assert not next(r for r in ng.results if r.number == "3").passed


def test_family_gate_short_worst_trade(seeded):
    m = _good_metrics() | {"worst_trade": -0.30, "mdd": 0.10}
    rep = evaluate(m, strategy_id="s", family="SHORT_US", horizon_days=10, k_index=1,
                   control_expectancies=[0.0] * 100, fold_expectancies=[0.01] * 10,
                   is_sharpe=1.0, oos_sharpe=1.0)
    assert any(r.number == "F2" and not r.passed for r in rep.results)


def test_family_gate_lev_mdd_is_tighter(seeded):
    m = _good_metrics() | {"mdd": 0.18}      # 공통 20% 는 통과, LEV 15% 는 실패
    common = evaluate(m, strategy_id="l", family="LONG", horizon_days=5, k_index=1,
                      control_expectancies=[0.0] * 100, fold_expectancies=[0.01] * 10,
                      is_sharpe=1.0, oos_sharpe=1.0)
    lev = evaluate(m | {"sideways_expectancy": 0.0}, strategy_id="lv", family="LEV_ETF",
                   horizon_days=3, k_index=1, control_expectancies=[0.0] * 100,
                   fold_expectancies=[0.01] * 10, is_sharpe=1.0, oos_sharpe=1.0,
                   family_context={"sideways_expectancy": 0.0})
    assert next(r for r in common.results if r.number == "4").passed
    assert any(r.number == "F1" and not r.passed for r in lev.results)


# ---------------------------------------------------------------- #12 승률 정의
def test_win_rate_counts_closed_only(seeded):
    import pandas as pd

    from app.backtest.engine import compute_metrics

    trades = pd.DataFrame({
        "closed": [1, 1, 0],
        "pnl_pct": [0.05, -0.02, 0.99],       # 미청산 큰 수익은 세면 안 된다
        "hold_days": [3, 4, 5],
    })
    m = compute_metrics(trades, pd.DataFrame(), 1.0)
    assert m["n_trades"] == 3
    assert m["n_closed"] == 2
    assert m["win_rate"] == pytest.approx(0.5)


# ---------------------------------------------------------------- #25 예측 착시
def test_wilson_ci_width_shrinks_with_n():
    lo30, hi30 = wilson_ci(15, 30)
    lo300, hi300 = wilson_ci(150, 300)
    assert (hi30 - lo30) > (hi300 - lo300)
    assert (hi30 - lo30) > 0.30, "n=30 승률 CI 는 대략 ±18%p 여야 한다"


def test_cluster_bootstrap_widens_ci_vs_naive():
    """같은 날 종목들은 상관이 높다 → 클러스터 CI 가 더 넓어야 한다."""
    rng = np.random.default_rng(3)
    days, vals, clusters = 20, [], []
    for d in range(days):
        shared = rng.normal(0, 0.4)                    # 그날 공통 충격
        for _ in range(10):
            vals.append(shared + rng.normal(0, 0.05))
            clusters.append(f"day{d}")
    v = np.array(vals)
    lo_c, hi_c = cluster_bootstrap_ci(v, np.array(clusters))
    lo_n, hi_n = cluster_bootstrap_ci(v, np.arange(len(v)))   # 클러스터 없음 = 순진한 CI
    assert (hi_c - lo_c) > (hi_n - lo_n)


def test_random_predictions_have_no_edge_in_bull_market(seeded):
    """상승장에서 무작위 예측의 W_pred 는 B_pred 와 거의 같아야 한다 (#25)."""
    from app.backtest.engine import PriceBook
    from app.data.pit_store import PITStore
    from app.predictions.score import baseline

    store = PITStore()
    end = dt.date(2020, 6, 30)
    book = PriceBook(store.prices(end, start=dt.date(2019, 1, 1)))
    syms = ["AAPL", "MSFT", "NVDA", "JPM", "XOM", "JNJ", "CAT", "LMT"]
    days = [PriceBook.to_ord(d) for d in [dt.date(2020, 3, 2), dt.date(2020, 4, 1), dt.date(2020, 5, 1)]]
    b_up = baseline(book, syms, days, 5, +1)
    # 같은 유니버스에서 무작위로 상승 예측 → 적중률이 기준선과 통계적으로 같아야 한다
    assert 0.0 <= b_up <= 1.0
    rng = np.random.default_rng(11)
    picks = [(rng.choice(syms), d) for d in days for _ in range(8)]
    hits = []
    for sym, d in picks:
        from app.predictions.score import _nth_bar

        t = _nth_bar(book, str(sym), d, 5)
        base_px = book.last_close(str(sym), d)
        if t and base_px:
            hits.append(int(t[1] > base_px))
    w_pred = float(np.mean(hits)) if hits else float("nan")
    assert abs(w_pred - b_up) < 0.35, "무작위 예측이 기준선과 크게 다르면 채점 로직이 이상하다"
