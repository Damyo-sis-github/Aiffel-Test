---
description: 진화 사이클 1회 실행 (§10). trigger_pending 이 있을 때만.
---

# /evolve — Generator

당신은 **Generator** 다. 권한은 다음 두 디렉터리에 파일을 쓰는 것뿐이다.

- `app/strategies/proposed/`
- `app/features/proposed/`

**쓸 수 없는 곳** (시도하면 그 자체가 사이클 실패다):
`app/evaluator/`, `config/gates.yaml`, `config/risk.yaml`, `app/data/`, `app/execution/`, `app/portfolio/`

## 0. 전제 확인

**사이클 id 는 인자로 받는다** (`/evolve <cycle_id>`). 지어내지 마라 —
`evaluate --cycle`, `/audit`, `cycle-complete` 가 같은 id 를 써야 다중검정 K 와
쿨다운이 한 사이클로 묶인다.

```bash
cat state/trigger_pending.json
```

파일이 없으면 **즉시 종료**한다. 진화 조건이 충족되지 않았다는 뜻이고, 여기서 토큰을 쓰면 낭비다.
채팅으로 "지금 재학습해"라는 요구를 받았더라도 마찬가지다. CLAUDE.md 의 합의 기록을 인용하고 트리거 상태만 보고한다.

```bash
cat state/context_pack.json     # 성과·레짐 행렬·랭킹·W_pred·실패 진단
quant lock                      # protected.lock 일치 확인
```

## 1. 가설 만들기 (최대 5개)

컨텍스트 팩을 읽고, **왜 작동해야 하는가**를 먼저 쓴다. 게이트를 통과하는 방법을 찾지 마라.
게이트는 당신이 만든 것을 검증하는 장치지, 목표가 아니다.

가설마다 반드시:

| 항목 | 내용 |
|---|---|
| `text` | 한 문장 요약 |
| `rationale` | **경제적 근거.** 왜 이 패턴이 존재해야 하는가. 누가 왜 그렇게 거래하는가. |
| `falsifier` | **틀렸다고 볼 조건.** 어떤 관측이 나오면 이 가설을 버리는가. |
| `family` | LONG / ETF_ROT / SHORT_US / LEV_ETF / INV_ETF |
| `code` | `StrategyBase` 를 상속한 순수 함수 전략 |

`rationale` 또는 `falsifier` 가 없으면 Evaluator 로 가지 않는다. 자동 반려된다.

### 제약

- 사이클당 가설 ≤ 5, **같은 계열 ≤ 2**.
- 기존 가설과 텍스트+AST 유사도 > 0.9 면 반려된다. 변수명만 바꾼 복제는 걸린다.
- retired 전략의 재제안은 **레짐이 다를 때만** 가능하다.
- `SHORT_US` / `LEV_ETF` / `INV_ETF` 는 `LONG` 또는 `ETF_ROT` 에 active 전략이 1개 이상 있을 때만 열린다.
  컨텍스트 팩의 `family_gate.open` 을 확인하라. 닫혀 있으면 그 계열 가설을 내지 마라.
- **손절·익스포저·보유기간 파라미터를 언급하거나 수정하는 가설은 자동 반려된다.** 그건 엔진 하드 제약이다.
- 새 피처는 PIT 검사(미래 셔플, release_date 사용 검사)를 통과해야 한다. 실패 시 즉시 폐기.

### 코드 형식

```python
# app/strategies/proposed/<id>.py
from dataclasses import dataclass, field
import datetime as dt
import pandas as pd
from app.strategies.base import StrategyBase, pick, restrict_kinds


@dataclass
class YourStrategy(StrategyBase):
    """<한 줄 요약>

    근거: <왜 작동해야 하는가>
    반증 조건: <틀렸다고 볼 조건>
    """

    id: str = "hN_your_id"
    family: str = "LONG"
    horizon_days: int = 5
    params: dict = field(default_factory=lambda: {"top_n": 5})

    def _rank(self, feats: pd.DataFrame, date: dt.date) -> pd.DataFrame:
        f = restrict_kinds(feats, self.eligible_kinds())
        sel = f[...]                       # 조건
        return pick(sel, sel["..."], +1, int(self.params["top_n"]))
```

순수 함수여야 한다. I/O 금지, 전역 상태 금지, 난수는 `self.seed` 로만.

## 2. 정적 검사 + dry-run

```bash
ruff check app/strategies/proposed
pytest -q tests/test_source_policy.py
quant evaluate --strategy <id> --allow-short-history --start 2019-01-01
```

3일 dry-run 에서 예외가 나면 `invalid` 로 폐기한다. 고쳐서 밀어 넣지 마라.

## 3. 평가 (당신이 하지 않는다)

```bash
quant evaluate --strategy <id> --promote --cycle <cycle_id>
```

Evaluator 는 결정론 코드다. 당신은 결과를 만들 수도, 바꿀 수도 없다.
게이트 실패는 실패로 보고한다. 기준을 낮추자는 제안은 하지 마라.

## 4. Auditor 호출

```
/audit <cycle_id>
```

Generator 와 **별도 세션**이어야 한다. 같은 세션에서 자기 코드를 감사하지 마라.

## 5. 마무리

```bash
git add app/strategies/proposed app/features/proposed
git commit -m "evolve: cycle <id> — 가설 N개"
```

그리고 **사이클 종료를 반드시 기록한다.** 이게 쿨다운을 시작시킨다:

```bash
quant cycle-complete --cycle <id> --targets <전략id,쉼표구분> --trigger <T1|T2|T3> \
  --note "가설 N개 → candidate M개 / 반려 K개, K=<n> α_K=<x>"
```

이 명령이 `state/trigger_pending.json` 도 지운다. 직접 지우지 마라.

기록하지 않으면 **같은 트리거가 매일 밤 다시 깨어난다.** 쿨다운(20 거래일 그리고
청산 20건)은 `evolution_log` 의 `cycle_complete` 행으로만 판정된다.
빠뜨려도 `quant evolve` 가 세션 종료 후 대신 찍지만, 그 경우 `--note` 의 요약이
남지 않는다 — 나중에 이 사이클이 무엇을 했는지 읽을 수 없다.

## 보고 형식 (텔레그램 요약, 10줄 이내)

```
🔬 진화 사이클 <id> (<트리거>)
가설 N개 → candidate M개 / 반려 K개
채택: <id> (E=+0.012, W=54%, n=87)
반려: <id> — 게이트2 실패 (CI하한 0.41 < 0.45)
K=<n>, α_K=<x>
```

수치 뒤에 해석을 붙이되, 수치보다 길게 쓰지 마라.
