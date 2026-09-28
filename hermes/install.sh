#!/usr/bin/env bash
# Hermes 운영 등록 — Hermes 가 도는 머신에서 한 번 실행한다 (다시 실행해도 안전).
#   bash hermes/install.sh
# 하는 일:
#   1. 파이썬 의존성 설치, .env 준비, Claude Code CLI 확인
#   2. ~/.hermes/scripts/ 에 no-agent 크론 스크립트 3개 생성 (저장소 경로·python·claude 경로 고정)
#   3. ~/.hermes/skills/skin-marketing/ 에 텔레그램 답장 처리 스킬 설치
#   4. hermes cron 작업 등록 (이미 있으면 건너뜀)
# 환경변수로 바꿀 수 있는 값: HERMES_HOME, PYTHON, CLAUDE_BIN, HERMES_DELIVER(기본 telegram)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PYTHON="${PYTHON:-$(command -v python3 || command -v python)}"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || true)}"
DELIVER="${HERMES_DELIVER:-telegram}"
warn() { echo "⚠️  $*"; }

echo "저장소: $REPO"
echo "python: $PYTHON"

# 1. 준비 -----------------------------------------------------------------
"$PYTHON" -m pip install -q -r "$REPO/requirements.txt" || warn "pip 설치 실패 — 가상환경이면 PYTHON=<venv python> 으로 다시 실행"

if [ ! -f "$REPO/.env" ]; then
  cp "$REPO/.env.example" "$REPO/.env"
  chmod 600 "$REPO/.env"
  warn ".env 를 만들었습니다. GEMINI_API_KEY, LAW_API_OC 값을 채워주세요: $REPO/.env"
  warn "(Hermes 에 등록한 Gemini 키는 크론 스크립트로 전달되지 않아 여기에 따로 넣어야 합니다)"
fi

if [ -z "$CLAUDE_BIN" ]; then
  warn "claude 명령을 찾지 못했습니다. Claude Code 설치·로그인 후 CLAUDE_BIN=<경로> 로 다시 실행하세요."
  CLAUDE_BIN="claude"
fi

# 2. 크론 스크립트 --------------------------------------------------------
mkdir -p "$HERMES_HOME/scripts"
write_script() {  # $1=파일명 $2=본문
  cat > "$HERMES_HOME/scripts/$1" <<EOF
#!/usr/bin/env bash
# skin-marketing — hermes/install.sh 가 생성. 수정은 저장소의 install.sh 에서.
set -euo pipefail
cd "$REPO"
export PATH="$(dirname "$CLAUDE_BIN"):$(dirname "$PYTHON"):\$PATH"
export CLAUDE_BIN="$CLAUDE_BIN"
# .env 의 값이 있는 항목만 환경변수로 (빈 값이 기존 환경변수를 덮지 않게)
if [ -f .env ]; then
  while IFS='=' read -r key value; do
    case "\$key" in ''|\#*) continue ;; esac
    if [ -n "\$value" ]; then export "\$key=\$value"; fi
  done < .env
fi
$2
EOF
  chmod +x "$HERMES_HOME/scripts/$1"
  echo "스크립트: $HERMES_HOME/scripts/$1"
}

# 주간 주제: 먼저 코드 업데이트 (law_sync 가 갱신한 legal/ 은 버리고 받음 — 다음 동기화 때 다시 생성됨)
write_script skin-topics.sh "git checkout -q -- legal/ 2>/dev/null || true
git pull -q --ff-only >&2 || echo '(코드 업데이트 실패 — 기존 코드로 진행)' >&2
exec \"$PYTHON\" -m pipeline.01_topics --from-seed 6"  # 초기 주제 목록 소진 후 자동으로 Gemini 조사

write_script skin-worker.sh "exec \"$PYTHON\" -m pipeline.worker run"

write_script skin-law-sync.sh "exec \"$PYTHON\" -m pipeline.law_sync"

# 블로그: 승인된 글이 바뀌었을 때만 Cloudflare Pages 배포 (없으면 조용)
write_script skin-site.sh "exec \"$PYTHON\" -m pipeline.site deploy"

# 링크 유입 주간 요약 (TRACKER_URL 미설정이면 조용히 끝남)
write_script skin-traffic.sh "exec \"$PYTHON\" -m pipeline.tracker report --days 7"

# 월간 유입 분석 (영업용 데이터셋·리포트, 추적기 미설정·클릭 없으면 조용)
write_script skin-analytics.sh "exec \"$PYTHON\" -m pipeline.analytics report"

# 점검용 (크론 아님): 크론과 같은 환경에서 키·모델·claude 확인
write_script skin-check.sh "\"$PYTHON\" -m pipeline.llm check || true
\"$PYTHON\" -m pipeline.sponsors check || true
\"$CLAUDE_BIN\" -p 'Reply with exactly: ok' || echo 'claude -p 실패 — Claude Code 로그인 확인'"

# 3. 스킬 -----------------------------------------------------------------
mkdir -p "$HERMES_HOME/skills/skin-marketing"
sed -e "s#{{REPO}}#$REPO#g" -e "s#{{PYTHON}}#$PYTHON#g" \
  "$REPO/hermes/skills/skin-marketing/SKILL.md" > "$HERMES_HOME/skills/skin-marketing/SKILL.md"
echo "스킬: $HERMES_HOME/skills/skin-marketing/SKILL.md"

# 4. 크론 등록 ------------------------------------------------------------
if ! command -v hermes >/dev/null 2>&1; then
  warn "hermes 명령이 없어 크론 등록을 건너뜁니다. hermes/README.md 의 명령으로 직접 등록하세요."
  exit 0
fi
existing="$(hermes cron list 2>/dev/null || true)"
add_job() {  # $1=이름 $2=스케줄 $3=스크립트
  if grep -q -- "$1" <<<"$existing"; then
    echo "크론 있음: $1 (건너뜀)"
  else
    hermes cron create "$2" --no-agent --script "$3" --deliver "$DELIVER" --name "$1"
    echo "크론 등록: $1 ($2)"
  fi
}
add_job skin-law-sync "every monday 8am" skin-law-sync.sh
add_job skin-weekly-topics "every monday 9am" skin-topics.sh
add_job skin-worker "every 10m" skin-worker.sh
add_job skin-traffic-report "every monday 10am" skin-traffic.sh
add_job skin-site "every 1h" skin-site.sh
add_job skin-analytics "0 11 1 * *" skin-analytics.sh   # 매월 1일 11:00 (지난달)

cat <<EOF

완료. 확인:
  1. $REPO/.env 에 GEMINI_API_KEY, LAW_API_OC 입력
  2. bash "$HERMES_HOME/scripts/skin-check.sh"   (키·Claude Code 점검)
  3. hermes cron run skin-weekly-topics   (지금 바로 주제 후보 받아보기)
EOF
