"""§13.1 학습 모드 — 용어 설명 + 지표별 한 줄 해석.

규칙
  - 처음 등장하는 용어만 3줄 이내로 설명하고, 이후엔 반복하지 않는다.
  - 설명 이력은 reports/glossary_seen.json 에 남는다.
  - 해석은 수치 **뒤**에, 수치보다 **길지 않게**.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.paths import reports_dir

SEEN_FILE = "glossary_seen.json"

# §2 용어 사전
GLOSSARY: dict[str, str] = {
    "호라이즌": "보유 기간. ML 의 '라벨 예측 창'에 해당한다.",
    "백테스트": "과거 데이터로 돌려보는 시뮬레이션. 오프라인 평가와 같다.",
    "워크포워드": "시간순으로 굴리며 검증하는 방식. 시계열 교차검증이다.",
    "IS/OOS": "학습 구간 / 한 번도 안 쓴 구간. train/test 와 같다.",
    "룩어헤드": "미래 정보가 새어 들어온 것. data leakage 다.",
    "생존편향": "상장폐지된 종목이 빠져서 성과가 좋아 보이는 착시.",
    "슬리피지": "주문가와 실제 체결가의 차이.",
    "MDD": "고점 대비 최대 낙폭. 최악의 순간이 얼마나 아팠는지.",
    "샤프": "수익을 변동성으로 나눈 값. 신호 대 잡음비와 같다.",
    "레짐": "시장 국면. 분포가 바뀌는 것(distribution shift)이다.",
    "유니버스": "거래 대상 종목 집합.",
    "벤치마크": "비교 기준 지수. 이걸 못 이기면 그냥 지수를 사는 게 낫다.",
    "페이퍼 트레이딩": "가짜 돈으로 실시간 매매. 온라인 평가다.",
    "상대강도": "A 가 B 보다 얼마나 더 올랐는가.",
    "폭": "바스켓 안에서 오른 종목의 비율.",
    "공매도": "빌려서 팔고 떨어지면 되사는 것. 손실 상한이 없다.",
    "대차수수료": "빌린 주식에 붙는 이자(연 %).",
    "숏스퀴즈": "공매도가 몰린 종목이 급등해 강제 청산이 연쇄되는 현상.",
    "레버리지 ETF": "기초지수 일일 수익의 2~3배를 따라가는 ETF.",
    "인버스 ETF": "기초지수 일일 수익의 −1~−2배를 따라가는 ETF.",
    "변동성 감쇠": "레버리지·인버스 ETF 가 매일 리밸런싱해서 횡보장에서 녹는 현상.",
    "명목 익스포저": "레버리지를 반영한 실제 노출. 2배 ETF 를 10% 사면 노출은 20%.",
    "기대값": "이길 확률×평균이익 − 질 확률×평균손실 − 비용. 이게 0 보다 커야 시작이다.",
    "W_pred": "매일 낸 예측 리스트의 적중률.",
    "B_pred": "우연 기준선. 같은 기간 유니버스 전체가 그 방향으로 움직인 비율.",
    "CI": "신뢰구간. 표본이 작으면 넓어지고, 넓으면 아직 아무 말도 못 한다.",
    "부트스트랩": "데이터를 재표집해 불확실성을 재는 방법.",
    "다중검정": "여러 번 시도하면 우연히 좋은 게 나온다. 그래서 기준을 조인다.",
    "킬스위치": "낙폭이 한도를 넘으면 전부 멈추는 장치.",
    "PIT": "Point-in-Time. 그 시점에 실제로 알 수 있었던 정보만 쓰는 것.",
}


@dataclass
class GlossaryTracker:
    path: Path | None = None

    def _file(self) -> Path:
        return self.path or (reports_dir() / SEEN_FILE)

    def seen(self) -> set[str]:
        p = self._file()
        if not p.exists():
            return set()
        try:
            return set(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            return set()

    def new_terms(self, text: str, limit: int = 2) -> list[tuple[str, str]]:
        """리포트에 처음 등장하는 용어를 최대 limit 개 뽑는다."""
        seen = self.seen()
        out = []
        for term, desc in GLOSSARY.items():
            if term in seen:
                continue
            if term in text:
                out.append((term, desc))
            if len(out) >= limit:
                break
        return out

    def mark(self, terms: list[str]) -> None:
        p = self._file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(sorted(self.seen() | set(terms)), ensure_ascii=False, indent=1), encoding="utf-8")


# ------------------------------------------------------------------ 한 줄 해석


def interpret(metric: str, value: float, *, n: int | None = None, extra: dict | None = None) -> str:
    """지표별 한 줄 해석. 수치보다 길지 않게."""
    extra = extra or {}
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "아직 값이 없습니다."

    if metric == "win_rate":
        if n is not None and n < 30:
            return f"표본이 적어({n}건) 아직 의미 없습니다."
        if value >= 0.5:
            return "기준(50%) 위입니다. 다만 손익비를 같이 봐야 합니다."
        return "기준(50%) 아래입니다."
    if metric == "expectancy":
        return "거래당 기대값이 양수입니다." if value > 0 else "거래당 기대값이 음수입니다 — 1차 게이트 실패."
    if metric == "sharpe":
        return "변동성 대비 수익이 준수합니다." if value >= 0.5 else "변동성에 비해 수익이 약합니다."
    if metric == "mdd":
        thr = float(extra.get("threshold", 0.20))
        return f"한도 {thr:.0%} 안입니다." if value <= thr else f"한도 {thr:.0%}를 넘었습니다."
    if metric == "edge":
        return "우연 기준선을 넘었습니다." if value > 0 else "우연 기준선 아래 — 신호 품질 경고."
    if metric == "gross_notional_pct":
        return f"명목 노출 {value:.0%}. 상한 {float(extra.get('threshold', 1.5)):.0%} 대비 여유가 있습니다." \
            if value <= float(extra.get("threshold", 1.5)) else "명목 노출이 상한에 닿았습니다."
    if metric == "drawdown":
        return "정상 범위입니다." if abs(value) < float(extra.get("threshold", 0.15)) else "킬스위치 한도에 근접했습니다."
    return ""
