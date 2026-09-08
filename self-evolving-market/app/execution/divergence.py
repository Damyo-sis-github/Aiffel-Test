"""§9 괴리 추적 — 백테스트 가정 체결가 vs 페이퍼 체결가.

전략별 20거래 이동평균 괴리 > 0.3% 면 quarantine 한다.

지금 이것이 **무엇을 재는가** (정직하게)
  이 저장소에는 `PaperBroker` 하나뿐이고, 백테스트와 페이퍼는 같은 `CostModel.fill`
  을 쓴다. 그래서 두 값이 구조적으로 같아야 **정상**이다. 여기서 재는 것은 실전
  슬리피지가 아니라 **두 경로가 갈라졌는지**다.

  갈라질 수 있는 지점이 실제로 있다:
    - `opening` 판정. 엔진은 신규 진입 경로에서 True 로 두고, 페이퍼는 보유 여부
      (`held is None`)로 정한다. 페이퍼 상태가 어긋나면 비용 구간이 달라진다.
    - `is_short` 판정. 역시 페이퍼는 보유 포지션의 부호를 본다.
    - `adv20`. 같은 PriceBook 을 쓰지만 조회 시점이 다르면 달라질 수 있다.

  즉 괴리 > 0 은 슬리피지가 아니라 **상태 드리프트 신호**다. 그래서 quarantine 이
  맞는 대응이다 — 그 전략의 페이퍼 성과를 더 이상 믿을 수 없다는 뜻이니까.

  Phase 4 에서 KIS 모의투자가 붙으면 같은 지표가 진짜 체결 괴리도 함께 잰다.
  그때 이 파일의 계산식은 바뀌지 않는다. 비교 대상만 늘어난다.

§15 실전 전환 조건에 "괴리 quarantine 0" 이 들어 있다. 이 숫자가 0 이 아니면
실전 전환을 논할 수 없다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.backtest.costs import CostModel
from app.backtest.engine import PriceBook
from app.universe.snapshot import kind_of

WINDOW_TRADES = 20
THRESHOLD = 0.003


def assumed_fill_price(
    costs: CostModel, book: PriceBook, order, day_ord: int, ref_px: float
) -> float | None:
    """백테스트가 같은 주문에 대해 가정했을 체결가.

    엔진의 신규 진입 경로와 **같은 인자**로 부른다 (opening=True, is_short 는 주문
    방향에서). 페이퍼가 보유 상태를 보고 다르게 판단했다면 여기서 값이 갈린다.
    """
    if ref_px is None or ref_px <= 0:
        return None
    f = costs.fill(
        ref_price=ref_px,
        qty=order.qty,
        side=order.side,
        opening=True,
        market=order.market,
        kind=kind_of(order.symbol),
        is_short=order.side < 0,
        adv20=book.adv20(order.symbol, day_ord),
    )
    return float(f.price)


def relative_gap(actual: float, assumed: float | None) -> float:
    """|실제 − 가정| / 가정. 가정가가 없거나 0 이면 NaN (0 으로 채우지 않는다)."""
    if assumed is None or not np.isfinite(assumed) or assumed == 0:
        return float("nan")
    return abs(float(actual) - float(assumed)) / abs(float(assumed))


def rolling_gap(trades: pd.DataFrame, *, window: int = WINDOW_TRADES) -> pd.Series:
    """전략별 최근 `window` 거래의 평균 괴리. 표본이 모자란 전략은 빼지 않고 NaN 으로 둔다.

    NaN 을 0 으로 채우면 "아직 모른다"가 "괜찮다"로 바뀐다. 그건 다른 말이다.
    """
    if trades is None or trades.empty:
        return pd.Series(dtype=float)
    df = trades.dropna(subset=["bt_px"]).copy()
    if df.empty:
        return pd.Series(dtype=float)
    df["gap"] = [
        relative_gap(a, b)
        for a, b in zip(df["fill_px"].astype(float), df["bt_px"].astype(float), strict=True)
    ]
    df = df.sort_values("fill_date")
    out = {}
    for sid, g in df.groupby("strategy_id", sort=True):
        tail = g["gap"].tail(window).dropna()
        out[sid] = float(tail.mean()) if len(tail) >= window else float("nan")
    return pd.Series(out, dtype=float)


def breaches(gaps: pd.Series, *, threshold: float = THRESHOLD) -> list[str]:
    """격리해야 할 전략 id. NaN(표본 부족)은 위반이 아니다."""
    if gaps.empty:
        return []
    return sorted(str(sid) for sid, v in gaps.items() if np.isfinite(v) and v > threshold)


def summarize(gaps: pd.Series, quarantined: list[str], *, threshold: float = THRESHOLD) -> str:
    """리포트 한 줄. 재지 못했으면 '이상 없음'이 아니라 '표본 부족'이라고 쓴다."""
    if gaps.empty:
        return "측정 대상 거래 없음"
    measured = gaps.dropna()
    if measured.empty:
        return f"전 전략 표본 부족 (전략당 {WINDOW_TRADES}거래 필요) — 아직 판단할 수 없음"
    worst_id = str(measured.idxmax())
    line = (f"측정 {len(measured)}/{len(gaps)}개 전략 · 최대 {measured.max() * 100:.3f}% "
            f"({worst_id}) · 한도 {threshold * 100:.1f}%")
    return line + (f" · 격리 {len(quarantined)}건: {', '.join(quarantined)}" if quarantined else " · 격리 없음")


def check(
    trades: pd.DataFrame, *, window: int = WINDOW_TRADES, threshold: float = THRESHOLD
) -> tuple[pd.Series, list[str], str]:
    gaps = rolling_gap(trades, window=window)
    bad = breaches(gaps, threshold=threshold)
    return gaps, bad, summarize(gaps, bad, threshold=threshold)
