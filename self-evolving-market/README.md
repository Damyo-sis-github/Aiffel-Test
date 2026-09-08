# 자가 발전형 시장 리서치 프로그램 (명세 v0.5)

한국·미국 시장 데이터를 매일 흡수하고, 전략 풀을 스스로 평가·진화시켜,
다음 성장 시장(테마 → 국가·지역 → 개별 종목)을 조기 포착하는 **개인 자산 운용 준비용 실험 플랫폼**.

> **실계좌 주문 없음.** 백테스트 + 페이퍼 트레이딩 전용. 투자 자문이 아닙니다.
> 실전 전환은 명세 §15 조건을 전부 충족한 뒤 **사람이** 결정합니다.
> 이 저장소에는 실전 주문 코드가 존재하지 않으며, 존재하면 CI 가 실패합니다.

---

## 지금 어디까지 되어 있나

| Phase | 범위 | 상태 |
|---|---|---|
| 0 | 데이터 레이어(PIT), 무결성 게이트, 유니버스 4종, exclusions, 알림, 스케줄러, healthcheck, catchup | ✅ 구현·테스트 완료 |
| 1 | 백테스트 엔진(도구별 비용·하드 제약), 시드 전략 전 계열, 워크포워드, 공통+계열 게이트, random_ctrl | ✅ 구현·테스트 완료 |
| 2 | 페이퍼 시뮬 두 계좌, 예측 리스트·채점, 일일 프로토콜, 리포트, replay, 카카오톡 | ✅ 구현·테스트 완료 |
| 3 | 자가발전 루프(트리거·컨텍스트 팩·유사도·Curator·계열 순서·다중검정), `/evolve`·`/audit` | ✅ 결정론 부분 완료. LLM 부분은 슬래시 커맨드 |
| 4 | 토스 API 어댑터, HMM, 뉴스 빈도, KIS 모의투자, Deflated Sharpe, KR 대주 공매도 | ⬜ 미착수 (선택) |

**현재 기본값은 `offline: true` — 합성 데이터로 동작합니다.** 실데이터 전환 절차는 아래 참조.

---

## 빠른 시작

```bash
cd self-evolving-market
uv venv .venv --python 3.11 && . .venv/bin/activate
uv pip install -e ".[dev]"                 # 실데이터까지: ".[dev,market]"

export QUANT_ROOT=$PWD                     # Windows: set QUANT_ROOT=%CD%
export QUANT_GUARD_BYPASS=1                # 아직 기기·네트워크 등록 전이므로

python -m app.cli backfill --date 2020-12-31 --start 2015-01-01
python -m app.cli lock --update --ack --reason "최초 봉인"
python -m app.cli daily --date 2020-06-01 --skip-guards
cat reports/daily/2020-06-01.md

python -m app.cli report --open           # 대시보드 HTML 하나를 만들고 브라우저로 엽니다
```

`report` 는 **서버를 띄우지 않습니다.** 로컬 웹서버는 포트를 열고 그건 회사 네트워크에서
감지됩니다(§11.8). 외부 CDN 도 쓰지 않아 오프라인에서 열립니다. 읽기 전용이라
대시보드에서 게이트를 통과시키거나 상태를 바꿀 수 있는 경로는 없습니다.

**열어두고 쓰려면** `--watch` 를 붙입니다:

```bash
python -m app.cli report --watch --open      # 기본 300초마다 스스로 다시 읽음
```

상주 프로세스가 아닙니다. 페이지에 `<meta http-equiv="refresh">` 가 들어갈 뿐이고,
새 내용은 `daily`(장 마감 후)와 `healthcheck`(매시)가 파일을 다시 만들면서 생깁니다.
띄워둔 페이지는 다음 주기에 그걸 읽어갑니다.

화면 상단에 **자기 나이**가 항상 표시되고, 하루가 넘으면 빨간 배너로 바뀝니다.
자동 갱신되는 화면이 조용히 어제 숫자를 보여주는 것은 수동 화면보다 나쁘기 때문입니다.

전략 평가:

```bash
python -m app.cli evaluate --start 2016-01-01 --date 2020-12-31 --control-size 20
```

게이트 실패가 정상입니다. 명세 §18: *"비용 차감 후 OOS 에서 대부분 전략은 실패한다. 실패 로그가 자산이다."*

---

## 실데이터로 전환하기

1. `uv pip install -e ".[market]"`
2. `.env.example` → `.env` 복사 후 채우기 (권한 600)
3. **휴장일 테이블 확정** — 이게 틀리면 백테스트가 조용히 어긋납니다:
   ```bash
   python -m app.cli calendar --refresh-kr --from 2013 --to 2027
   ```
4. `config/runtime.yaml` 에서 `data_sources.offline: false`
5. `python -m app.cli backfill --start 2015-01-01`

---

## 실행 조건 (§11.1, #31·#33·#34, §11.8)

`daily`/`evolve` 는 **전부 만족할 때만** 실행됩니다. 하나라도 어긋나면 실행하지 않고 텔레그램으로 사유를 보냅니다.

| 가드 | 설정 | 확인 |
|---|---|---|
| 등록된 기기 | `config/allowed_hosts.yaml` | `quant healthcheck --show-host` |
| 집 네트워크 | `config/allowed_networks.yaml` | `quant healthcheck --show-network` |
| 비동기화 경로 | `C:\dev\quant` (OneDrive·문서·바탕 화면 밖) | `quant healthcheck` |
| **기기 가시성** | `config/device_policy.yaml` | **`quant audit --device`** |
| 시계 오차 ≤ 60s | `config/runtime.yaml` | `quant healthcheck` |
| `protected.lock` 일치 | — | `quant lock` |

전부 **fail-closed** 입니다. 화이트리스트가 비어 있으면 통과가 아니라 거부입니다.
`QUANT_GUARD_BYPASS=1` 은 개발 전용이고, 켜지면 모든 리포트에 배너가 박힙니다.

### 기기 가시성 감사 (§11.8)

**"회사가 이 기기에서 무엇을 볼 수 있는가"** 를 레벨로 잽니다.

| 레벨 | 상태 | 회사가 보는 것 | 폴더·네트워크로 막을 수 있나 |
|---|---|---|---|
| 0 | 연결 없음 | — | — |
| 1 | 계정 경로 동기화 (OneDrive·KFM) | **그 폴더 안 파일 전체** | ✅ 저장소를 밖에 두면 됨 |
| 2 | 기기 등록 (WorkplaceJoined 등) | 기기명·OS·모델·로그인 시각 | ❌ 무관 |
| 3 | MDM (Intune) | 앱 목록·정책·원격 스크립트 | ❌ 무관 |
| 4 | EDR / 문서보안(DLP·DRM) | **프로세스 실행 기록·파일 접근** | ❌ 무관 |

```bash
quant audit --device                    # 지금 레벨 확인
quant audit --device --accept-baseline  # 현재 상태를 승격 감시 기준선으로 저장
```

명세 §11.8 의 원문 전제는 **레벨 ≤ 1** 이고 `device_policy.yaml` 기본값도 그렇습니다.
레벨 2 를 허용하려면 `overrides.allow_registered_without_mdm` 을 **사람이 근거를 적고** 켜야 합니다.
레벨 3·4 는 `never_allow` 에 있어 어떤 예외로도 넘을 수 없습니다.

> **승격 감시**: 레벨 2 는 조용히 레벨 3 이 될 수 있습니다. 회사가 Intune 을 활성화하면
> 이미 등록된 기기는 **사용자 동의 없이** MDM 으로 승격됩니다.
> 그래서 `daily` 는 매 실행마다 레벨을 재확인하고, 지난 기준선보다 올라가면 **즉시 중단**합니다.

---

## 운영 (Windows, 서버 없음)

```powershell
powershell -ExecutionPolicy Bypass -File windows\register_tasks.ps1
```

| 시각 (KST) | 작업 | LLM |
|---|---|---|
| 로그온 시 | `daily --catchup` (밀린 날 전부) | 없음 |
| 평일 16:30 | `daily --market kr` | 없음 |
| 화~토 07:00 | `daily --market us` | 없음 |
| 매일 19:00 | `evolve --if-pending` | Claude Code 헤드리스 |
| 토 09:00 / 매월 1일 09:00 | `weekly` / `monthly` | 없음 |
| 매시 | `healthcheck` (+ 대시보드 갱신) | 없음 |

**상시 가동은 필요 없습니다.** 페이퍼 체결가는 "다음날 시가"라는 과거 확정값이라,
노트북이 5일 꺼져 있다가 켜지면 `--catchup` 이 5일치를 순서대로 처리하고 결과는 매일 돌린 것과 **같습니다**.
이것은 주장이 아니라 테스트로 강제됩니다 (`tests/test_idempotency.py::test_catchup_matches_daily_runs`).

`evolve` 는 평일 08:00–18:00 KST 에는 거부하고 이월합니다 (본인이 다른 개발에 Claude 를 쓰는 시간).
`trigger_pending.json` 이 없으면 즉시 종료하므로 크레딧 소모가 0 입니다.

---

## 사람 게이트 4곳 (자동화 불가, 코드로 강제)

| | 무엇 | 명령 |
|---|---|---|
| **G1** | 킬스위치 해제 | `quant resume --ack` |
| **G2** | `evaluator/`·`gates.yaml`·`risk.yaml` 변경 승인 | `quant lock --update --ack` |
| **G3** | 데이터 게이트 "중단" 후 재개 | `quant resume --data --ack` |
| **G4** | 실전 전환 | 이 저장소엔 실전 주문 코드가 없음 (§15) |

---

## 구조

```
app/
  cli.py                  daily weekly monthly report replay resume lock healthcheck audit evolve backfill evaluate calendar
  guards/                 host network path clock protected llm_window     ← 실행 전 fail-closed 검사
  data/                   adapters/{synthetic,market_sources,toss} pit_store integrity ingest meta_db schema
  universe/               snapshot exclusions
  features/               price relstrength macro regime theme country screener builder
  predictions/            publish score (W_pred, B_pred, 클러스터 CI)
  strategies/             base registry seed/{long,tool}_strategies  proposed/(진화 산출물)
  backtest/               engine costs walkforward
  evaluator/              gates family_gates stats                   ← protected.lock 봉인
  portfolio/              accounts allocation risk killswitch
  execution/              broker(Protocol) paper_sim                 ← PaperBroker 만 존재
  evolve/                 trigger context_pack curator similarity
  pipeline/               daily positions state
  reports/                daily periodic dashboard(정적 HTML, 서버·CDN 없음)
  alerts/
config/                   gates risk costs etf_universe themes exclusions universe_seed
                          runtime allowed_hosts allowed_networks llm_window alerts holidays_kr protected.lock
windows/                  register_tasks.ps1 run_daily.cmd run_evolve.cmd run_task.cmd
.claude/commands/         evolve.md audit.md                          ← LLM 호출은 이 두 파일뿐
.codex/prompts/           review.md                                   ← 리뷰어는 Codex
scripts/codex_review.sh
tests/                    213개. §12 의 #1~#34 를 항목별로 강제
```

---

## 설계 원칙 (위반 시 PR 거부)

1. 평가기는 결정론 코드다. LLM 은 평가 결과를 만들거나 바꿀 수 없다.
2. 모든 상태는 디스크에. 채팅 맥락·프로세스 메모리 의존 금지.
3. 모든 변경은 git 커밋 + `evolution_log` (append-only, SQLite 트리거로 강제).
4. 멱등성. 같은 날짜 `daily` 재실행 = 같은 결과.
5. 신호 t → 체결 t+1. 예외 없음.
6. 사람 게이트 4곳 고정.
7. 회사 장비·네트워크·계정에서 실행하지 않는다.

---

## 리뷰

이 저장소의 코드 리뷰어는 **Codex** 입니다.

```bash
npm install -g @openai/codex && codex login
bash scripts/codex_review.sh --full
```

체크리스트는 `.codex/prompts/review.md` — §12 의 34개 안전장치를 우선순위대로 봅니다.
Codex 를 쓸 수 없는 환경이라면 그 사실을 리포트에 **명시**하고, 다른 모델의 리뷰를 Codex 리뷰라고 부르지 않습니다.
(이 저장소의 최초 구현은 Codex 접근이 차단된 환경에서 만들어졌습니다. `docs/review/` 참조.)

---

## 알려진 한계 — 숨기지 않습니다

| 항목 | 현재 상태 |
|---|---|
| 데이터 | 기본값이 **합성 데이터**입니다. 리포트 상단에 SYNTHETIC 배너가 박힙니다. |
| KR 휴장일 | `config/holidays_kr.yaml` 은 **미검증 시드값**입니다. `calendar --refresh-kr` 로 확정해야 합니다. 미검증 동안 캘린더 게이트는 중단 대신 플래그로만 동작합니다. |
| mcap | 실데이터가 아니라 거래대금 기반 근사입니다. 순위용으로만 씁니다 (Phase 4). |
| 대차수수료 | 상수 연 2% 근사입니다 (Phase 4 실데이터). |
| 익스포저 상한 | **진입 시점** 검사입니다. 진입 후 가격 변동으로 사후에 상한을 조금 넘을 수 있습니다 (§8.2 "리스크 체크는 신호 후·주문 전"). |
| 조정가 | 소스가 시점별 `adj_factor` 를 주지 않으면(yfinance) 배당 조정에 미세한 누출이 남습니다. 피처를 전부 비율로 만들어 상쇄했지만 완전하지는 않습니다. |
| B_pred | 계산 비용 때문에 최근 60일 표본으로 근사합니다. |
| 거래세·수수료 | `config/costs.yaml` 초기값입니다. 2026 실값 확인 필요 (§17). |
| MDD 15% | **임시값**입니다. 실전 전 본인이 숫자로 확정해야 합니다 (§15). |

---

## 현실 체크 (명세 §18)

- ACC_L 청산 100거래까지 약 3~6개월. **그 전 숫자는 결론이 아닙니다.**
- 도구를 다 열면 진화는 "잘 되는 것"보다 "위험한 것"에 먼저 끌립니다. 계열 순서·익스포저 상한·5일 제한이 그걸 막습니다. 이 셋을 풀면 안전장치 절반이 사라집니다.
- 100만 원 계좌는 통계용이 아니라 "이 신호가 내 돈으로 실제 실행 가능한가"를 보는 용도입니다. 두 계좌가 크게 다르면 그게 정보입니다.
- 비용 차감 후 OOS 에서 대부분 전략은 실패합니다. **실패 로그가 자산입니다.**
- 회사 장비에 올리는 것은 얻는 게 없고 잃을 것만 있습니다.
