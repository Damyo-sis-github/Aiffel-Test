#!/usr/bin/env bash
# Codex 코드 리뷰 하네스.
#
#   bash scripts/codex_review.sh            # 워킹 트리 변경분
#   bash scripts/codex_review.sh --staged   # 스테이지된 변경분
#   bash scripts/codex_review.sh --full     # 저장소 전체
#   bash scripts/codex_review.sh --since HEAD~5
#
# 결과는 docs/review/codex-<타임스탬프>.md 에 저장된다.
#
# 사전 조건
#   npm install -g @openai/codex
#   codex login            (또는 OPENAI_API_KEY 설정)
#   → OpenAI 도메인으로 나가는 아웃바운드가 막힌 환경에서는 실행할 수 없다.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROMPT_FILE=".codex/prompts/review.md"
OUT_DIR="docs/review"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$OUT_DIR/codex-$STAMP.md"
MODE="worktree"
SINCE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --staged) MODE="staged"; shift ;;
    --full)   MODE="full"; shift ;;
    --since)  MODE="since"; SINCE="$2"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
  esac
done

if ! command -v codex >/dev/null 2>&1; then
  cat >&2 <<'MSG'
codex CLI 를 찾을 수 없습니다.

  npm install -g @openai/codex
  codex login

리뷰어를 Codex 로 고정하는 것이 이 저장소의 규칙입니다(CLAUDE.md).
Codex 를 쓸 수 없는 환경이라면 그 사실을 리포트에 명시하고,
다른 모델의 리뷰를 Codex 리뷰라고 부르지 마십시오.
MSG
  exit 127
fi

mkdir -p "$OUT_DIR"

case "$MODE" in
  worktree) DIFF="$(git diff)"; SCOPE="워킹 트리 변경분" ;;
  staged)   DIFF="$(git diff --cached)"; SCOPE="스테이지된 변경분" ;;
  since)    DIFF="$(git diff "$SINCE")"; SCOPE="$SINCE 이후 변경분" ;;
  full)     DIFF=""; SCOPE="저장소 전체" ;;
esac

if [[ "$MODE" != "full" && -z "$DIFF" ]]; then
  echo "리뷰할 변경분이 없습니다 ($SCOPE)."
  exit 0
fi

TMP="$(mktemp -t codex-review-XXXXXX.md)"
trap 'rm -f "$TMP"' EXIT

{
  cat "$PROMPT_FILE"
  echo
  echo "---"
  echo
  echo "## 리뷰 범위: $SCOPE"
  echo
  if [[ "$MODE" == "full" ]]; then
    echo "저장소 전체를 리뷰한다. 진입점은 app/cli.py 이고,"
    echo "가장 위험한 코드는 app/data/pit_store.py, app/backtest/engine.py,"
    echo "app/evaluator/, app/pipeline/daily.py, app/pipeline/positions.py 다."
    echo "필요한 파일을 직접 열어 읽어라."
  else
    echo '```diff'
    echo "$DIFF"
    echo '```'
  fi
} > "$TMP"

echo "Codex 리뷰 시작 ($SCOPE)…"
codex exec --skip-git-repo-check - < "$TMP" | tee "$OUT"

echo
echo "리뷰 결과: $OUT"
