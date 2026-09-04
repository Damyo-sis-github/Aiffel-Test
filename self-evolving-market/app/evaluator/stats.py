"""통계 도구. 전부 결정론적(시드 고정)이어야 한다.

§1.2 판정 최소 표본: 30 청산 거래 예비, 100 청산 거래 공식.
§10.3-5 다중검정: 누적 K, α_K = 0.05/√K. **K 리셋 없음.**
"""

from __future__ import annotations

import math

import numpy as np

BOOTSTRAP_SEED = 20260903


def wilson_ci(successes: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """승률 신뢰구간 (Wilson). 정규근사보다 작은 n 에서 정확하다."""
    if n <= 0:
        return (float("nan"), float("nan"))
    z = _z_for(confidence)
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _z_for(confidence: float) -> float:
    table = {0.90: 1.6448536269, 0.95: 1.9599639845, 0.99: 2.5758293035}
    if confidence in table:
        return table[confidence]
    # Acklam 근사 대신 이분법 (결정론적, 의존성 없음)
    lo, hi = 0.0, 10.0
    target = (1 + confidence) / 2
    for _ in range(200):
        mid = (lo + hi) / 2
        if 0.5 * (1 + math.erf(mid / math.sqrt(2))) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def alpha_for_k(base_alpha: float, k_index: int) -> float:
    """α_K = base / √K. K 는 누적 시행 횟수 (§10.3-5)."""
    k = max(1, int(k_index))
    return float(base_alpha) / math.sqrt(k)


def bootstrap_p_value(
    observed: float,
    control_values: list[float] | np.ndarray,
    *,
    iters: int = 2000,
    seed: int = BOOTSTRAP_SEED,
) -> float:
    """대조군 분포 대비 관측값의 우측 p-value.

    control_values 는 random_ctrl 앙상블 각각의 같은 지표(보통 expectancy).
    p = (대조군 재표본 평균이 관측값 이상인 비율). 대조군이 비면 1.0 (통과 불가).
    """
    ctrl = np.asarray([c for c in control_values if c == c], dtype=float)
    if ctrl.size == 0 or not np.isfinite(observed):
        return 1.0
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, ctrl.size, size=(iters, ctrl.size))
    means = ctrl[idx].mean(axis=1)
    # +1 보정: 관측값이 대조군 최대보다 커도 p=0 이 되지 않게 한다.
    return float((np.sum(means >= observed) + 1) / (iters + 1))


def cluster_bootstrap_ci(
    values: np.ndarray,
    clusters: np.ndarray,
    *,
    confidence: float = 0.95,
    iters: int = 2000,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:
    """§1.2 일 단위 클러스터 부트스트랩.

    W_pred 는 하루 N개씩 쌓이지만 같은 날 종목들은 상관이 높다 → 유효 표본이 작다.
    날짜를 통째로 재표집해 그 상관을 반영한다.
    """
    v = np.asarray(values, dtype=float)
    c = np.asarray(clusters)
    ok = np.isfinite(v)
    v, c = v[ok], c[ok]
    if v.size == 0:
        return (float("nan"), float("nan"))
    uniq = np.unique(c)
    if uniq.size < 2:
        return (float(v.mean()), float(v.mean()))
    groups = [v[c == u] for u in uniq]
    rng = np.random.default_rng(seed)
    means = np.empty(iters, dtype=float)
    for i in range(iters):
        pick = rng.integers(0, len(groups), size=len(groups))
        sample = np.concatenate([groups[j] for j in pick])
        means[i] = sample.mean() if sample.size else np.nan
    lo, hi = np.nanpercentile(means, [(1 - confidence) / 2 * 100, (1 + confidence) / 2 * 100])
    return (float(lo), float(hi))


def effective_sample_size(clusters: np.ndarray) -> float:
    """클러스터 구조를 반영한 유효 표본 근사 (리포트용)."""
    c = np.asarray(clusters)
    if c.size == 0:
        return 0.0
    _, counts = np.unique(c, return_counts=True)
    return float(c.size / (counts.mean() if counts.mean() > 0 else 1.0))


def deflated_sharpe(sharpe: float, n_trials: int, n_obs: int) -> float:
    """Phase 4 예정. 지금은 시행 횟수를 반영한 보수적 조정값만 돌려준다."""
    if n_trials <= 1 or n_obs <= 1 or not np.isfinite(sharpe):
        return float(sharpe)
    penalty = math.sqrt(2 * math.log(max(n_trials, 2))) / math.sqrt(n_obs)
    return float(sharpe - penalty)
