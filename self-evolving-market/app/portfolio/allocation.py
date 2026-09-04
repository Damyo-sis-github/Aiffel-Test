"""§8.2 자본 배분: 활성 전략 균등 → 126일 샤프 비례. 전략당 <= 40%, 계열당 <= 50%."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.config import risk as load_risk


def allocate(
    active: list[str],
    families: dict[str, str],
    sharpe_126: dict[str, float] | None = None,
    *,
    days_running: int | None = None,
) -> dict[str, float]:
    """전략별 자본 비중. 합은 1.0 (현금 하한은 리스크 엔진이 따로 강제한다)."""
    if not active:
        return {}
    cfg = load_risk()["allocation"]
    per_strategy_max = float(cfg["per_strategy_max_pct"])
    per_family_max = float(cfg["per_family_max_pct"])
    warmup = int(cfg.get("warmup_days", 126))

    n = len(active)
    equal = {s: 1.0 / n for s in active}
    use_sharpe = (
        str(cfg.get("method", "")) == "sharpe_proportional"
        and sharpe_126
        and (days_running is None or days_running >= warmup)
    )
    if not use_sharpe:
        w = equal
    else:
        # 음의 샤프는 0 으로. 전부 0 이면 균등으로 되돌린다.
        raw = {s: max(0.0, float(sharpe_126.get(s, 0.0) or 0.0)) for s in active}
        total = sum(raw.values())
        w = {s: v / total for s, v in raw.items()} if total > 0 else equal

    # 정규화 → 종목 상한 → 계열 상한을 **한 루프 안에서** 반복한다.
    # 정규화를 마지막에 한 번만 하면 계열 상한이 다시 부풀어 무력화된다(실제로 그랬다).
    for _ in range(50):
        w = _normalize(w) or equal
        capped = _cap(w, per_strategy_max)
        capped = _cap_by_group(capped, families, per_family_max)
        if _within(capped, families, per_strategy_max, per_family_max) and abs(sum(capped.values()) - 1) < 1e-9:
            return capped
        w = capped
    return _normalize(w) or equal


def _normalize(w: dict[str, float]) -> dict[str, float] | None:
    total = sum(w.values())
    return {k: v / total for k, v in w.items()} if total > 0 else None


def _within(w: dict[str, float], groups: dict[str, str], cap: float, gcap: float) -> bool:
    if any(v > cap + 1e-9 for v in w.values()):
        return False
    by_group: dict[str, float] = {}
    for k, v in w.items():
        by_group[groups.get(k, "")] = by_group.get(groups.get(k, ""), 0.0) + v
    return all(t <= gcap + 1e-9 for t in by_group.values())


def _cap(w: dict[str, float], cap: float) -> dict[str, float]:
    """상한 초과분을 여유 있는 항목에 비례 재분배."""
    out = dict(w)
    for _ in range(20):
        over = {k: v for k, v in out.items() if v > cap + 1e-12}
        if not over:
            break
        excess = sum(v - cap for v in over.values())
        room = {k: v for k, v in out.items() if v < cap - 1e-12}
        if not room:
            break
        base = sum(room.values()) or 1.0
        for k in over:
            out[k] = cap
        for k, v in room.items():
            out[k] = min(cap, v + excess * v / base)
    return out


def _cap_by_group(w: dict[str, float], groups: dict[str, str], cap: float) -> dict[str, float]:
    """계열 합이 상한을 넘으면 그 계열을 축소하고, 남은 몫을 여유 계열에 비례 배분한다."""
    out = dict(w)
    for _ in range(20):
        by_group: dict[str, float] = {}
        for k, v in out.items():
            by_group[groups.get(k, "")] = by_group.get(groups.get(k, ""), 0.0) + v
        over = {g: t for g, t in by_group.items() if t > cap + 1e-12}
        if not over:
            break
        freed = 0.0
        for g, total in over.items():
            scale = cap / total
            for k in [k for k in out if groups.get(k, "") == g]:
                freed += out[k] * (1 - scale)
                out[k] *= scale
        room_groups = {g: t for g, t in by_group.items() if t < cap - 1e-12}
        room_base = sum(room_groups.values())
        if room_base <= 0 or freed <= 0:
            break
        for k, v in list(out.items()):
            g = groups.get(k, "")
            if g in room_groups and by_group[g] > 0:
                share = freed * (by_group[g] / room_base) * (v / by_group[g])
                out[k] = min(v + share, cap)
    return out


def sharpe_from_nav(nav: pd.DataFrame, lookback: int = 126) -> float:
    if nav is None or nav.empty or len(nav) < 20:
        return float("nan")
    v = nav["nav"].astype(float).to_numpy()[-(lookback + 1):]
    if len(v) < 20:
        return float("nan")
    r = np.diff(v) / v[:-1]
    r = r[np.isfinite(r)]
    if len(r) < 10 or r.std(ddof=1) == 0:
        return float("nan")
    return float(r.mean() / r.std(ddof=1) * np.sqrt(252))
