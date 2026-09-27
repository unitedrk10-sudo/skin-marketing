#!/usr/bin/env bash
# Claude Code 검수 한 사이클 — Hermes 크론(no-agent)에서 auto-review 직후 실행.
# export → claude -p 로 검수 → import. 출력(새로 반영된 초안 요약)이 있으면 Hermes 가 03_review list 를 보낸다.
# 필요: 이 머신에 Claude Code CLI 설치·로그인(`claude` 명령), 저장소 루트에서 실행.
set -euo pipefail
cd "$(dirname "$0")/.."

packet=$(python -m pipeline.02c_external_review export)
case "$packet" in
  review-queue/pending/*.json) ;;
  *) exit 0 ;;  # 검수 대기 초안 없음
esac
packet_id=$(basename "$packet" .json)
mkdir -p logs review-queue/done
result="review-queue/done/${packet_id}.result.json"

# 읽기·쓰기만 허용 (셸 실행·웹 접근 불가). 모델은 CLAUDE_REVIEW_MODEL 로 바꿀 수 있다.
claude -p "검수 요청 ${packet} 을 처리해줘. CLAUDE.md 의 '검수 요청 처리' 절차와 파일 안 instructions 를 따르고, 결과는 ${result} 한 파일에만 써." \
  --allowedTools "Read" "Write" "Glob" \
  --disallowedTools "Bash" "WebFetch" "WebSearch" \
  ${CLAUDE_REVIEW_MODEL:+--model "$CLAUDE_REVIEW_MODEL"} \
  > "logs/claude_review_${packet_id}.log" 2>&1 || true

if [ ! -s "$result" ]; then
  rm -f "$packet"  # 요청을 버려 다음 실행 때 같은 초안을 다시 요청하게 한다
  echo "ERROR: Claude Code 검수 실패 — 결과 파일 없음 (logs/claude_review_${packet_id}.log). 다시 실행하면 재요청." >&2
  exit 1
fi
python -m pipeline.02c_external_review import "$result"
