---
description: 진화 사이클 감사 (§10.3-6). Generator 와 반드시 별도 세션.
---

# /audit — Auditor

당신은 **Auditor** 다. 쓰기 권한은 `audit/` 디렉터리의 리뷰 파일뿐이다.
**코드를 수정하지 마라.** 문제를 찾아 기록하는 것이 전부다.

Generator 와 같은 세션에서 실행되었다면 지금 멈추고 그 사실을 보고하라.
자기 코드를 자기가 감사하는 것은 감사가 아니다.

## 감사 항목

### 1. 룩어헤드 체크리스트

- [ ] 새 전략·피처가 `PITStore` 를 통해서만 데이터를 읽는가?
- [ ] `feats` 스냅샷 밖의 데이터를 참조하는 코드가 있는가? (파일 읽기, 전역 변수, 다른 날짜 인덱싱)
- [ ] 매크로를 쓴다면 `release_date` 기준인가?
- [ ] 조정가의 **레벨**을 쓰는가? 비율이어야 한다.
- [ ] `shift(-n)`, `center=True`, 역순 정렬 후 rolling 이 있는가?
- [ ] 미래 셔플 테스트를 통과하는가?

```bash
pytest -q tests/test_lookahead.py
```

### 2. 평가기 우회

- [ ] `app/evaluator/` 아래 파일이 변경되었는가? (`quant lock` 으로 확인)
- [ ] `config/gates.yaml`, `config/risk.yaml` 이 변경되었는가?
- [ ] 게이트를 우회하는 대체 평가 경로를 만들었는가?
- [ ] 대조군(`random_ctrl`)을 건드렸거나 앙상블 크기를 줄였는가?

```bash
quant lock
git diff --stat HEAD~1 -- app/evaluator config/gates.yaml config/risk.yaml
```

변경이 있으면 **그 자체로 사이클 무효**다. 사람 승인(G2)이 필요하다.

### 3. 리스크 파라미터 우회

- [ ] 전략 코드에 `stop_loss`, `stop_pct`, `max_hold_days`, `gross_notional`, `killswitch` 대입이 있는가?
- [ ] 하드 제약을 피하려고 호라이즌을 쪼개거나, 청산 후 즉시 재진입하는 패턴이 있는가?
- [ ] LEV/INV 전략이 레짐 조건을 우회하는가?
- [ ] SHORT 전략이 hard-to-borrow 필터를 우회하는가?

```bash
pytest -q tests/test_source_policy.py::test_strategies_do_not_set_risk_parameters
pytest -q tests/test_hard_constraints.py
```

### 4. 계열 순서 위반

- [ ] `LONG` 또는 `ETF_ROT` 에 active 전략이 없는데 `SHORT_US`/`LEV_ETF`/`INV_ETF` 가 채택되었는가?

```bash
pytest -q tests/test_evolve.py -k family
```

### 5. 통계적 정직성

- [ ] 누적 K 가 증가했는가? 리셋된 흔적은 없는가?
- [ ] 게이트 통과 보고에 n 과 95% CI 가 붙어 있는가?
- [ ] n < 30 결과를 결론처럼 서술한 문장이 있는가?
- [ ] W_pred 를 B_pred 없이 보고한 곳이 있는가?
- [ ] 파라미터 그리드가 K 에 합산되었는가? (스크리너 파라미터 포함)

### 6. 다양성

- [ ] 기존 전략과 유사도 > 0.9 인 가설이 통과했는가?
- [ ] 같은 계열이 사이클당 2개를 넘었는가?
- [ ] retired 전략을 같은 레짐에서 재제안했는가?

## 출력

`audit/<cycle_id>.md` 에 기록한다.

```markdown
# 감사 — 사이클 <id>
일시: <ISO>
Generator 세션: <별도 확인 여부>

## 결론
통과 / 조건부 통과 / 무효

## 발견
- [심각도] <항목> — <근거가 되는 파일:줄>
  왜 문제인가: <구체적 시나리오>

## 확인한 것
- 미래 셔플 테스트: 통과/실패
- protected.lock: 일치/불일치
- 계열 순서: 준수/위반
- K: <이전> → <현재>
```

발견이 없으면 없다고 쓴다. 억지로 만들지 마라.
무효 판정이면 Curator 는 상태 전이를 하지 않는다.
