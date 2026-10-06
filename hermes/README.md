# Hermes 운영 설정

## 사전 준비
- Python 3.11+, git, Claude Code (로그인) — 설치돼 있음
- **Node.js 20 이상** (Cloudflare 배포 도구 `npx wrangler` 용): https://nodejs.org 에서 LTS 설치 → 새 터미널에서 `node -v` 확인

## 설치 (Hermes 머신에서 한 번)
```bash
git clone https://github.com/unitedrk10-sudo/skin-marketing.git && cd skin-marketing
python -m venv .venv                       # 저장소 전용 파이썬 (Hermes 자체 venv 와 분리)
.venv/bin/python hermes/install.py         # Windows: .venv\Scripts\python hermes\install.py
# .env 에 GEMINI_API_KEY, LAW_API_OC 입력 후 — 설치 끝에 출력되는 skin-check 명령으로 키·모델·Claude Code 로그인 점검
hermes cron run skin-weekly-topics         # 바로 주제 후보 받아보기
```
- 크론 스크립트는 `<HERMES_HOME>/scripts/<크론 이름>.py` 로 생긴다 (Windows `%LOCALAPPDATA%\hermes`, 그 외 `~/.hermes`). `.sh` 가 아닌 `.py` 인 이유: Hermes 는 `.sh` 를 PATH 의 bash 로 실행하는데 Windows 에서는 그게 WSL 이라 실패한다. `.py` 는 Hermes 자신의 Python 이 실행하고, 스크립트가 저장소 `.venv` 파이썬으로 파이프라인을 돌린다.
- `.venv` 가 있으면 자동으로 그 파이썬을 쓴다. 다른 파이썬: `--python <경로>` (또는 `PYTHON` 환경변수). `bash hermes/install.sh` 도 같은 설치를 한다.
- Hermes 에 등록한 Gemini 키는 크론 스크립트에 전달되지 않는다 (Hermes 관리 자격 증명). 그래서 저장소 `.env`(git 제외, 권한 600)에 따로 넣는다.
- Claude Code 가 같은 머신에 설치·로그인돼 있어야 한다 (`claude -p` 로 검수). Anthropic API 키는 필요 없다.
- `install.py` 는 다시 실행해도 안전하다 (이미 있는 크론은 건너뜀, 스크립트·스킬은 덮어씀). 스크립트 내용을 바꾸려면 `install.py` 의 `JOBS` 를 고치고 다시 실행.

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
- 주제 추천에 글별 조회수 반영(선택): 같은 화면의 사이트 태그(site tag)를 `analytics_site_tag` 에, Cloudflare API 토큰(Account Analytics Read)·계정 ID 를 `.env` 의 `CF_API_TOKEN`·`CF_ACCOUNT_ID` 에. 없으면 관심도 사전값 + 링크 클릭만으로 추천. 확인: `python -m pipeline.demand`

## 링크 유입 추적기
`tracker/README.md` 대로 Cloudflare 에 배포하고 `.env` 에 `TRACKER_URL`, `TRACKER_TOKEN` 입력. 텔레그램에서 "글로우 링크 만들어줘" → 채널별 추적 링크.

## 크론 (no-agent — stdout 이 그대로 텔레그램, 출력 없으면 조용, 실패 시 에러 알림)
| 이름 | 스케줄 | 스크립트 | 하는 일 |
|---|---|---|---|
| skin-law-sync | 월 08:00 | `skin-law-sync.py` | 추적 법령 변경 감지 → 변경 있을 때만 알림 |
| skin-weekly-topics | 월 09:00 | `skin-weekly-topics.py` | 코드 업데이트(`git pull`) → 관광지 트렌드 스캔(주 1회) → 주간 주제 후보 전송 (수요·트렌드·계절 순) |
| skin-worker | 10분마다 | `skin-worker.py` | 요청 처리(초안 생성·수정 재생성: Gemini 출처 찾기 → Claude 작성) → 자동 검수(Gemini) → 바뀐 게 있으면 검수 요청 전송 |
| skin-traffic-report | 월 10:00 | `skin-traffic-report.py` | 스폰서 링크 유입 주간 요약 (추적기 배포 전·클릭 없으면 조용) |
| skin-weekly-report | 일 20:00 | `skin-weekly-report.py` | 주간 리포트 (초안·검수 등급·승인·렌더링·게시·대기·자동/사람 일치율·블로그·유입·수요 상위) |
| skin-clinics | 월 07:00 | `skin-clinics.py` | 코스 관광지 주변 피부과 목록 갱신 (심평원 공공데이터, `DATA_GO_KR_KEY` 없으면 조용) |
| skin-analytics | 매월 1일 11:00 | `skin-analytics.py` | 지난달 유입 분석 → `reports/analytics/<월>.md·.csv` (시술별 의도·도착, 영업 벤치마크) |
| skin-site | 매시 | `skin-site.py` | 승인된 블로그 글이 바뀌었을 때만 사이트 빌드·배포 → "새 글" 알림 |

스크립트는 `<HERMES_HOME>/scripts/` 에 생성된다. 수동 실행: `hermes cron run <이름>`.

## 텔레그램 대화 (에이전트 + `skin-marketing` 스킬)
`<HERMES_HOME>/skills/skin-marketing/SKILL.md` 가 답장을 명령으로 바꾼다.

```
월 09:00  [주제 후보 6건]            ← skin-weekly-topics
사람      "1,3,4"                    → worker request-drafts (요청만 남김)
~20분 후  [초안 검수 3건] ✅⚠️⛔     ← skin-worker (출처 확보 → claude -p 작성 → 02b 검수) + 초안별 .md 첨부
사람      "1,3 승인 / 2 수정: …"     → 03_review apply (수정은 요청만 남김)
~10분 후  [초안 검수 1건]            ← skin-worker (재생성 → 재검수)
```
- 검수 메시지에는 초안별 대본 전문·블로그 구성·걸린 항목이 들어가고, 블로그 전문과 사실별 원문 인용은 첨부 파일(`<draft_id>.md`)로 온다.
- Claude 작성(`claude -p`)은 Claude Code CLI 로그인이 필요하다. 사용량 한도에 걸리면 요청을 버리지 않고 다음 워커 실행 때 다시 한다 (실패 알림에 "보류"로 표시).
- 실패한 요청은 `content/requests/failed/` 로 옮겨져 반복 실행되지 않는다.
- 게시·삭제(06_publish) 등 되돌릴 수 없는 작업은 명령 승인(approval) 대상으로 등록한다 (구현 후).
- 영상: 승인된 초안은 워커가 `04_render_video` 로 렌더링(Gemini TTS — `GEMINI_API_KEY` + ffmpeg, 없으면 조용히 건너뜀) → "게시 전 확인" 메시지 → "게시 OK" → 워커가 `06_publish kits` 로 채널별 게시 키트 → 직접 올린 뒤 "N 게시 완료 <채널> <URL>" → 모두 올리면 published.
- 05_preview 는 따로 없다: `03_review list --stage rendered` 메시지 + 스킬이 영상 파일을 첨부한다.
- 예약 게시 도구·플랫폼 API 연결은 아직 (지금은 키트로 수동 게시).
