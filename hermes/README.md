# Hermes 운영 설정

## 사전 준비
- Python 3.11+, git, Claude Code (로그인) — 설치돼 있음
- **Node.js 20 이상** (Cloudflare 배포 도구 `npx wrangler` 용): https://nodejs.org 에서 LTS 설치 → 새 터미널에서 `node -v` 확인

## 설치 (Hermes 머신에서 한 번)
```bash
git clone https://github.com/unitedrk10-sudo/skin-marketing.git && cd skin-marketing
bash hermes/install.sh
# .env 에 GEMINI_API_KEY, LAW_API_OC 입력 후
bash ~/.hermes/scripts/skin-check.sh      # 키·모델·Claude Code 로그인 점검
hermes cron run skin-weekly-topics         # 바로 주제 후보 받아보기
```
- Hermes 에 등록한 Gemini 키는 크론 스크립트에 전달되지 않는다 (Hermes 관리 자격 증명). 그래서 저장소 `.env`(git 제외, 권한 600)에 따로 넣는다.
- Claude Code 가 같은 머신에 설치·로그인돼 있어야 한다 (`claude -p` 로 검수). Anthropic API 키는 필요 없다.
- `install.sh` 는 다시 실행해도 안전하다 (이미 있는 크론은 건너뜀). 스크립트 내용을 바꾸려면 저장소의 `install.sh` 를 고치고 다시 실행.

## 스폰서(광고주 병원) 등록
```bash
cp config/sponsors.example.yaml config/sponsors.yaml   # git 제외 — 병원명·계약은 이 머신에만
# 병원명(영/한), 공식 사이트, 계약(monthly|per_post, 기간), 사전심의 여부·번호 입력
python -m pipeline.sponsors check
```
텔레그램: "글로우 스폰서 글: 리쥬란 안내" → 초안(광고 표시 자동) → 검수 → 병원에 최종본 전달 → 병원 OK 후 `N 병원확인` → `N 승인`.

## 블로그 사이트 (Cloudflare Pages)
```bash
npx wrangler login                                                    # 추적기와 같은 Cloudflare 계정
npx wrangler pages project create skin-site --production-branch main  # 한 번만
python -m pipeline.site build && python -m pipeline.site deploy       # 첫 배포 → https://skin-site.pages.dev
```
- 브랜드·도메인 확정 후 `config/site.yaml` 의 `name`, `domain` 을 바꾸고, Cloudflare Pages → Custom domains 에 도메인 연결.
- 도메인을 넣어야 `sitemap.xml`·canonical 이 생긴다 → Google Search Console·Bing Webmaster 에 sitemap 등록 (Bing 은 ChatGPT 검색의 주요 데이터원).
- 게시 대상은 사람이 승인한 글(approved 이후)만. SNS 프로필 링크에는 이 블로그 주소를 건다.
- 방문 통계: Cloudflare 대시보드 → Analytics & Logs → Web Analytics → 사이트 추가 → 발급된 토큰을 `config/site.yaml` 의 `analytics_token` 에 넣고 다시 배포 (쿠키 없음, 유입 경로에서 ChatGPT·Perplexity 등 AI 검색 유입 확인 가능).

## 링크 유입 추적기
`tracker/README.md` 대로 Cloudflare 에 배포하고 `.env` 에 `TRACKER_URL`, `TRACKER_TOKEN` 입력. 텔레그램에서 "글로우 링크 만들어줘" → 채널별 추적 링크.

## 크론 (no-agent — stdout 이 그대로 텔레그램, 출력 없으면 조용, 실패 시 에러 알림)
| 이름 | 스케줄 | 스크립트 | 하는 일 |
|---|---|---|---|
| skin-law-sync | 월 08:00 | `skin-law-sync.sh` | 추적 법령 변경 감지 → 변경 있을 때만 알림 |
| skin-weekly-topics | 월 09:00 | `skin-topics.sh` | 코드 업데이트(`git pull`) → 주간 주제 후보 전송 |
| skin-worker | 10분마다 | `skin-worker.sh` | 요청 처리(초안 생성·수정 재생성) → 자동 검수 → Claude Code 검수 → 바뀐 게 있으면 검수 요청 전송 |
| skin-traffic-report | 월 10:00 | `skin-traffic.sh` | 스폰서 링크 유입 주간 요약 (추적기 배포 전·클릭 없으면 조용) |
| skin-site | 매시 | `skin-site.sh` | 승인된 블로그 글이 바뀌었을 때만 사이트 빌드·배포 → "새 글" 알림 |

스크립트는 `~/.hermes/scripts/` 에 생성된다. 수동 실행: `hermes cron run <이름>`.

## 텔레그램 대화 (에이전트 + `skin-marketing` 스킬)
`~/.hermes/skills/skin-marketing/SKILL.md` 가 답장을 명령으로 바꾼다.

```
월 09:00  [주제 후보 6건]            ← skin-weekly-topics
사람      "1,3,4"                    → worker request-drafts (요청만 남김)
~10분 후  [초안 검수 3건] ✅⚠️⛔     ← skin-worker (생성 → 02b → claude -p 검수)
사람      "1,3 승인 / 2 수정: …"     → 03_review apply (수정은 요청만 남김)
~10분 후  [초안 검수 1건]            ← skin-worker (재생성 → 재검수)
```
- ⏳ = Claude Code 검수 대기. 결과 반영 전에는 승인이 보류된다 (사람이 명시하면 `--confirm`).
- Claude Code 검수가 실패하면 알림 후 다음 워커 실행 때 자동 재요청. 로그: `logs/claude_review_<id>.log`.
- 실패한 요청은 `content/requests/failed/` 로 옮겨져 반복 실행되지 않는다.
- 게시·삭제(06_publish) 등 되돌릴 수 없는 작업은 명령 승인(approval) 대상으로 등록한다 (구현 후).
- 미구현: `04_render_video`, `05_preview`, `06_publish`, `07_report` (TTS·합성·예약 게시 도구 선정 후).
