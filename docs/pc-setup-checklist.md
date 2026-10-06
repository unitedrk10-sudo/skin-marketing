# PC 작업 체크리스트 (Hermes 운영 PC)

모바일로 준비할 수 있는 것은 먼저 해 두고, 나머지는 PC에서 위에서부터 순서대로 진행한다.
각 단계 끝의 **확인** 명령이 통과하면 다음 단계로 넘어간다. 키·토큰은 `.env`에만 넣고 채팅·저장소에 붙이지 않는다.

## 0. 모바일로 미리 해둘 것
- [ ] 도메인 `skinboundkorea.com` 구매 (Cloudflare Registrar 권장 — DNS·Pages·Workers 가 한 계정에)
- [ ] Cloudflare → Email Routing → `hello@skinboundkorea.com` → 개인 메일로 전달
- [ ] 텔레그램 BotFather → 봇 생성 → 토큰은 메모만 해 두기 (PC 에서 Hermes 에 연결)
- [ ] 공공데이터포털(data.go.kr) → "건강보험심사평가원_병원정보서비스" 활용신청 (승인까지 시간이 걸릴 수 있음)
- [ ] SNS 계정 선점: TikTok·Instagram·YouTube `@skinboundkorea`
- [ ] Google Cloud 콘솔 → Text-to-Speech API 사용 설정 → API 키 발급 (월 100만 자 무료 한도)

## 1. 설치
- [ ] Node.js LTS 설치 (Python·git 은 설치되어 있음)
- [ ] ffmpeg 설치 — Windows `winget install Gyan.FFmpeg` / macOS `brew install ffmpeg`
- [ ] Claude Code CLI 로그인 (`claude` 실행 → Pro 계정) — 검수 단계가 `claude -p` 를 쓴다
- [ ] 저장소 최신화 + 의존성
  ```bash
  git pull
  pip install -r requirements.txt
  cd tracker && npm install && npm test && cd ..
  ```
- **확인**: `python -m pytest -q` 전부 통과

## 2. 키 입력 (`.env`, `.env.example` 참고)
| 키 | 어디서 | 없으면 |
|---|---|---|
| `GEMINI_API_KEY` | Google AI Studio (5만원 크레딧 계정) | 초안 생성 불가 (필수) |
| `LAW_API_OC` | 국가법령정보 Open API (등록 완료) | 법령 동기화 불가 |
| ~~`GOOGLE_TTS_API_KEY`~~ | 불필요 — 영상 음성은 Gemini TTS 로 `GEMINI_API_KEY` 사용 (2026-10-06) | - |
| `DATA_GO_KR_KEY` | data.go.kr (0단계 승인 후) | 코스 주변 병원 목록 없음 |
| `TRACKER_URL`, `TRACKER_TOKEN` | 5단계 배포 후 | 링크 추적 없음 (원래 주소로 연결) |
| `CF_API_TOKEN`, `CF_ACCOUNT_ID` | Cloudflare (Account Analytics Read) | 주제 추천에 조회수 미반영 |

- **확인**: `python -m pipeline.llm check`

## 3. 모델 점검
- [ ] `config/models.yaml` 의 Gemini 모델을 3.x 로 바꿀지 결정 → `python -m pipeline.llm check` 로 사용 가능 여부 확인
- [ ] `python -m pipeline.law_sync` 한 번 실행 (legal/digest.md 갱신)

## 4. 블로그 (Cloudflare Pages)
```bash
npx wrangler login
npx wrangler pages project create skin-site --production-branch main
python -m pipeline.site build && python -m pipeline.site deploy
```
- [ ] Pages → Custom domains → `skinboundkorea.com` 연결
- [ ] Web Analytics → 사이트 추가 → 토큰을 `config/site.yaml` `analytics_token`, 사이트 태그를 `analytics_site_tag` 에
- [ ] `config/site.yaml` `contact_email: hello@skinboundkorea.com`
- [ ] Google Search Console·Bing Webmaster Tools 에 도메인 등록 → `https://skinboundkorea.com/sitemap.xml` 제출
- **확인**: 사이트 접속, 소개·개인정보 페이지, 링크 미리보기 카드(카톡·슬랙에 주소 붙여보기)

## 5. 링크 추적기 (Cloudflare Worker) — `tracker/README.md`
- [ ] D1 생성 → `wrangler.toml` database_id → schema 적용 → `ADMIN_TOKEN`·`VISITOR_SALT` 시크릿 → `npx wrangler deploy`
- [ ] Workers → Domains 에 `go.skinboundkorea.com` 연결 → `.env` 의 `TRACKER_URL` 을 이 주소로
- **확인**: `python -m pipeline.tracker list` (빈 목록이면 정상)

## 6. Hermes 등록
```bash
python -m venv .venv                     # 이미 있으면 생략
.venv\Scripts\python hermes\install.py    # macOS/Linux: .venv/bin/python hermes/install.py
# 끝에 출력되는 skin-check 명령 실행 (키·모델·Claude Code 점검)
```
- [ ] 텔레그램 봇 연결·DM 페어링 (Hermes 메신저 게이트웨이)
- [ ] 크론 등록 확인: `hermes cron list` — law-sync, weekly-topics, worker, traffic-report, site, clinics, weekly-report, analytics
  - `skin-analytics` 의 `0 11 1 * *`(매월 1일) 형식이 안 되면 README 명령으로 직접 등록
- **확인**: 텔레그램에 봇이 답하는지, 실패 알림이 오는지 (일부러 키 하나 빼고 `hermes cron run skin-worker`)

## 7. 시험 운영
- [ ] `hermes cron run skin-weekly-topics` → 텔레그램에 주제 후보 (트렌드 스캔 포함)
- [ ] 3개 골라 답장 → 워커가 출처 확보·Claude 작성·자동 검수 → 검수 요청(첨부 파일 포함) → 샘플 3편 품질 확인
- [ ] 1편 승인 → 블로그 반영 확인 → 영상 렌더링 → "게시 OK" → 게시 키트(`kit.md`) 확인
- [ ] `python -m pipeline.clinics refresh` → `python -m pipeline.clinics show coex` (병원 목록이 실제로 나오는지)
- [ ] `python -m pipeline.demand` (조회수 반영 여부 — CF 키 넣은 경우)
- [ ] `python -m pipeline.07_report`

## 8. 게시 시작
- [ ] SNS 프로필 링크 = 블로그 주소 (스폰서 병원 링크 금지)
- [ ] 첫 1주는 하루 1편 이하로 올리며 플랫폼 AI 라벨·자막 확인
- [ ] 파일럿 병원 1~2곳: `config/sponsors.yaml` 등록 (`contract.type: pilot`, `zone`, 필요하면 `route_ad`) → `python -m pipeline.sponsors check`
