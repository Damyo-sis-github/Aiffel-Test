"""#1 룩어헤드 — 미래 셔플 테스트.

t 이후 데이터를 아무리 바꿔도 t 까지의 신호·피처가 변하면 안 된다.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from app.data.pit_store import PITStore, PITViolation
from app.features.builder import FeatureBuilder
from app.strategies.registry import seed_strategies

CUT = dt.date(2020, 6, 30)


def test_pit_store_requires_as_of(seeded):
    store = PITStore()
    with pytest.raises(PITViolation):
        store.prices(None)


def test_pit_filter_excludes_future(seeded):
    store = PITStore()
    df = store.prices(CUT, symbols=["AAPL"])
    assert df["event_date"].max() <= pd.Timestamp(CUT)
    assert df["as_of"].max() <= pd.Timestamp(CUT)


def test_macro_respects_release_date(seeded):
    """발표 전 지표를 보면 룩어헤드다. as_of == release_date 여야 한다."""
    store = PITStore()
    m = store.macro(CUT)
    assert not m.empty
    assert (m["as_of"] == m["release_date"]).all()
    assert m["release_date"].max() <= pd.Timestamp(CUT)
    # 발표 지연이 있는 시리즈는 event_date 가 cutoff 를 넘을 수 있다 — 그것이 PIT 의 핵심이다.
    assert (m["as_of"] <= pd.Timestamp(CUT)).all()


def test_future_shuffle_does_not_change_past_features(seeded):
    """미래 셔플: cutoff 이후 가격을 뒤섞어도 cutoff 까지의 피처는 동일해야 한다."""
    store = PITStore()
    fb = FeatureBuilder(store)
    before = fb.build(CUT, start=dt.date(2018, 1, 1)).features

    raw = store.read_unfiltered("prices")
    future = raw[raw["event_date"] > pd.Timestamp(CUT)].copy()
    assert len(future) > 100, "셔플할 미래 데이터가 있어야 의미 있는 테스트다"
    rng = np.random.default_rng(7)
    for col in ("open", "high", "low", "close"):
        future[col] = future[col].to_numpy() * rng.uniform(0.5, 2.0, len(future))
    future["high"] = future[["open", "high", "low", "close"]].max(axis=1)
    future["low"] = future[["open", "high", "low", "close"]].min(axis=1)
    store.write("prices", future.drop(columns=["year"], errors="ignore"))

    after = FeatureBuilder(PITStore()).build(CUT, start=dt.date(2018, 1, 1)).features
    cols = [c for c in before.columns if before[c].dtype.kind == "f"]
    pd.testing.assert_frame_equal(
        before[cols].sort_index(), after[cols].sort_index(), check_exact=False, atol=1e-9
    )


def test_signals_unchanged_after_future_shuffle(seeded):
    store = PITStore()
    panel = FeatureBuilder(store).build(CUT, start=dt.date(2018, 1, 1))
    snap = panel.on(CUT)
    strategies = [s for s in seed_strategies() if s.id != "random_ctrl"]
    baseline = {s.id: s.signals(snap, CUT).to_dict("records") for s in strategies}

    raw = store.read_unfiltered("prices")
    future = raw[raw["event_date"] > pd.Timestamp(CUT)].copy()
    future["close"] = future["close"] * 3.0
    future["high"] = future[["open", "high", "low", "close"]].max(axis=1)
    store.write("prices", future.drop(columns=["year"], errors="ignore"))

    snap2 = FeatureBuilder(PITStore()).build(CUT, start=dt.date(2018, 1, 1)).on(CUT)
    for s in strategies:
        assert s.signals(snap2, CUT).to_dict("records") == baseline[s.id], f"{s.id} 신호가 미래에 오염됨"


def test_panel_rejects_future_query(seeded):
    panel = FeatureBuilder().build(CUT, start=dt.date(2019, 1, 1))
    with pytest.raises(ValueError):
        panel.on(CUT + dt.timedelta(days=10))
