# CLAUDE.md

기획서: `docs/derm-content-automation.md` (전체 설계·법규 체크리스트의 기준 문서)
마케팅 프레임: `docs/marketing-framework.md` (타깃·차별점·브랜드·페이지·채널·성과 지표와 결정 대기 목록 — 화면·채널 작업 전에 먼저 확인)

## 작업 규칙
- 개발은 Claude Code 대화 세션으로, 운영은 Hermes가 한다. 운영 중 Claude 는 워커가 부르는 초안 작성 단계(`claude -p`, `llm.py` provider `claude_cli`)로만 쓴다. 스크립트는 사람 개입 없이 CLI로 단독 실행 가능해야 한다.
- 모든 LLM 호출(Claude CLI 포함)은 `pipeline/llm.py`를 통해서만 한다. 모델명은 `config/models.yaml`에서 읽는다.
- 역할 분담 (2026-10-06): **Gemini = 찾기(주제·트렌드·출처 페이지)·검수, Claude = 읽고 쓰기**. 같은 회사 모델이 쓰고 검수하지 않는다 (테스트로 강제).
- API 키·봇 토큰(`GEMINI_API_KEY`, `LAW_API_OC` 등)은 환경변수로만 읽고, 코드·로그·저장소에 남기지 않는다.
- 파일 상태 이동(drafts → approved → rendered → ready_to_publish → published)은 `03_review.py`와 정해진 스크립트만 수행한다.
- `06_publish.py`는 `ready_to_publish/` 외의 경로를 처리하지 않는다. 이 제약을 우회하는 코드를 만들지 않는다.
- 생성 프롬프트에는 기획서 7장 금지 사항과 "모든 사실에 출처 URL" 규칙을 반드시 포함한다.
- 각 스크립트는 실패 시 0이 아닌 종료 코드와 원인 로그를 남긴다 (Hermes 실패 알림용).
- 법령 근거가 필요한 규칙은 `legal/digest.md`(국가법령정보 API 수집본)를 기준으로 하고, 추적 대상은 `config/laws.yaml`에 추가한다.

## 명령
- 테스트: `python -m pytest -q`
- 법령 조회: `python -m pipeline.law_api search 의료법` / `python -m pipeline.law_api article 의료법 27`
- 법령 동기화: `python -m pipeline.law_sync`
- LLM 설정 점검: `python -m pipeline.llm check`
- 주간 주제: **목요일 09:00 다음 주 게시용** (`--next-week`, 2026-10-10 — 목·금 주제 선택 → 초안 → 토·일 검수 → 월·수·금 게시). 토·일 10시 이후 다음 주 빈 게시일이 있으면 워커가 알림(`site.weekend_check`). `python -m pipeline.01_topics` / 초기 주제 목록에서: `--from-seed 6` (`config/seed_topics.yaml`, 소진 시 Gemini 로 자동 전환). 추천 순서 = 수요 점수 (`python -m pipeline.demand`: `config/procedures.yaml`·`config/attractions.yaml` 관심도 사전값 + 글별 조회수·링크 클릭, 같은 시술·관광지 주 2개까지)
- 주제 구성: `config/channels.yaml` `topic_quota` (주 6개 = 시술 정보 4 + 시술 × 여행 2), 유형 안에서는 수요 점수 순 (`01_topics.pick`). `content_axes` 에 없는 축의 주제는 후보에서 빠진다 — 관광지만 다루는 `travel_guide` 는 2026-10-06 제외.
- 관광지 데이터(시술 × 여행 글용): `config/attractions.yaml` 속성으로 코스 규칙을 프롬프트에 넣는다 (`pipeline/attractions.py`, 관찰일 가능 목록 `python -m pipeline.attractions`)
- 관광지 트렌드: `python -m pipeline.trends scan|show` — 주 1회 Gemini 검색 스캔(01_topics 가 자동 실행, `content/trends/`), 관광지 점수에 트렌드(+최대 0.4)·계절(`season`, 이번·다음 달 +0.15) 가산, 신규 장소는 주제 조사 프롬프트의 수요 정보로만(주제 후보로 직접 올리지 않음). 맛집·카페 트렌드(food)도 같이 스캔 → 근처 관광지 점수(×0.75)·시술 × 여행 글 프롬프트(출처 포함). 출처 없는 트렌드는 버리고 21일 지난 스캔은 무시. 식당·카페 이름은 독립 출처가 있는 무상 편집 예시로만 (`_rules.md`)
- 초안 생성: `python -m pipeline.02_draft --week 2026-W40 --pick 1,3` / 수정: `--revise <draft_id> --note "..."` — Gemini 출처 찾기(`sources`) → 페이지 확보·필터(`pipeline/web.py`) → Claude 작성(`write`, 페이지 5개 미만이면 `write_search`) → 원문 인용 대조
- 자동 검수: `python -m pipeline.02b_auto_review --auto-regenerate` (출처 대조·교차 검수 = Gemini). ⛔ → 자동 재생성 1회, ⚠️ 이고 문장으로 고칠 지적(교차 검수·출처 근거 약함)이 있으면 사람에게 보내기 전에 지적된 문장만 자동 수정 1회(`02_draft.fix`, `prompts/fix_draft.md`, 버전마다 1회) → 재검수
- (선택, 현재 미사용) Claude Code 외부 검수 한 사이클: `python -m pipeline.02c_external_review review` — 검수 단계를 `claude_code` 로 바꿨을 때만
- 워커(Hermes 크론): `python -m pipeline.worker run` / 주제 선택 요청: `python -m pipeline.worker request-drafts --pick 1,3`
- 사람 검수: `python -m pipeline.03_review list` / `python -m pipeline.03_review apply "1,3 승인"`
- 스폰서 글: `python -m pipeline.02_draft --sponsor <id> --title "..." --angle "..."` (병원 권역 중심 여행 코스 글: `--course`, sponsors.yaml `zone` 필요) / 목록 점검 `python -m pipeline.sponsors check`
- 블로그 사이트: `python -m pipeline.site build|deploy|schedule` (site/dist → Cloudflare Workers 정적 자산). 배포 때 새 글·내용이 바뀐 글 주소를 IndexNow(`site.yaml indexnow_key`, Bing → ChatGPT 검색·Copilot)로 알리고, 내용이 바뀐 글은 수정일 표시·dateModified (`content/site/content_log.json`)
- AI 유입: `python -m pipeline.ai_traffic [--days 7]` — AI 답변에서 온 방문(Web Analytics 유입 도메인)·AI 봇 실시간 조회(ChatGPT-User 등, 토큰에 Zone Analytics Read 필요). 주간 리포트에 자동 포함
- 링크 유입 추적: `python -m pipeline.tracker add|list|report|sponsor-report` / Worker 테스트 `cd tracker && npm test` (`tracker/README.md`)
- 숏폼 렌더링: **2026-10-10 중단** (`shortform.format: off` — 수익이 나기 전까지 블로그·Threads·X 만). `python -m pipeline.04_render_video [--id <draft_id>]` (approved → rendered). 형식은 `config/channels.yaml` `shortform.format`:
  `video`(2026-10-07 기본, 시험 운영 주 `video_per_week` 2편) = Gemini TTS Kore(1.1배) + 실제 영상 클립 2.6초 컷(`pipeline/broll.py`: 직접 모은 클립 `content/library/videos/<장면>/` → Pixabay `PIXABAY_API_KEY`, 사람·병원 태그 제외, 줄마다 후보 썸네일 묶음을 Claude `pick_visuals` 가 고름) + 위 장면 제목·아래 단어 강조 자막 + 배경 음악(`content/library/music/` 에 있으면).
  `carousel`(보류 — 글자 슬라이드는 시험해 보니 약함) = 사진 넘기기형 `pipeline/carousel.py`, `--refresh <draft_id>` 로 상태 이동 없이 다시.
  주력은 블로그(구글 검색)·Threads·X 이고 숏폼은 작게 시험한다 — 같은 틀 영상을 많이 올리면 유튜브 "대량 생산·반복 콘텐츠"(2026-07)·스팸 판정에 가까워진다.
  게시 키트: `python -m pipeline.06_publish kits|status|done <id> <채널> <URL>` (ready_to_publish 전용 → published) / 주간 리포트: `python -m pipeline.07_report`
- X·Threads 글: `python -m pipeline.social compose|today|list|post <N|오늘> threads|done <N|오늘> <x|threads> <URL>` — 게시된 중립 블로그 글마다 3종(link 새 글 소개 · fact 사실 하나 · angle 여행 팁/흔한 오해, 사실 목록만, 02b 규칙 검사, 작성은 Claude `social`) + 일요일 한 주 정리(그 주 2편 이상, 코드가 제목으로). 매일 9시(KST, `channels.yaml text_social.daily`) 이후 워커가 "오늘의 SNS 글" 1개: 일요일 정리 → 안 보낸 link → fact·angle, 오늘 게시 예정 블로그 글이 안 올라왔으면 정오까지 대기. X 는 작성 링크로 사람이 게시(API 유료라 기본 꺼짐, 웹 자동 조작은 약관 위반이라 만들지 않음) — link·정리 글은 본문에 링크 없이 `done … x <URL>` 이 링크 답글 작성 링크(in_reply_to)를 돌려준다. Threads 는 무료 API (실패하면 오류만 알리고 다른 방법으로 올리지 않는다). 스폰서 글은 `channels.yaml sponsored_policy` 에서 x·threads 허용 전까지 제외
- 블로그 게시 일정: `config/site.yaml publish_schedule` (월·수·금 7시 KST, 하루 1편) — 승인된 글은 다음 빈 요일에 배정(`content/site/schedule.json` 고정), 확인 `python -m pipeline.site schedule`. 게시일에 올라갈 글이 없으면 워커가 전날 18시·당일 9시에 텔레그램 리마인드(`site.publish_reminder`)
- 유입 분석(영업용 데이터셋): `python -m pipeline.analytics report [--month YYYY-MM | --days 90]` → `reports/analytics/`
- Hermes 설치·운영: `hermes/README.md` (`.venv` 파이썬으로 `hermes/install.py`), 텔레그램 답장 스킬: `hermes/skills/skin-marketing/SKILL.md`

## 구조 메모
- 초안 = `content/<상태>/<draft_id>/` 폴더 (`draft.json`, `script.md`, `blog.md`, `review.json`, `history.json`, 텔레그램 첨부용 `<draft_id>.md`). `content/` 는 운영 데이터라 git 에 올리지 않는다.
- 대본·블로그는 02_draft 의 사실 목록(`facts`, 사실마다 출처 URL + 페이지 원문 인용 `quote`)만 사용하고 `[F#]` / `fact_ids` 로 참조한다. 인용문이 받아 둔 페이지에 없는 사실은 작성 직후 코드가 버린다(`web.quote_in_page`). 02b 는 이 참조를 기준으로 출처를 다시 검증한다.
- 출처 페이지: 병원 사이트로 보이는 도메인(`web.is_clinic_host`)은 수집 단계에서 빼고(스폰서 글의 광고주 공식 사이트만 예외), 그래도 들어오면 02b 가 ⚠️.
- 병원 공통 실무 정보 (중간안, 2026-10-06): Gemini 가 `clinic_pages` 로 병원 페이지를 따로 찾고(`fetch_clinic_pages`, 병원 3곳 미만이면 안 씀, 스폰서 글엔 안 씀), Claude 는 상담 절차·언어 지원·예약·패키지 구성 같은 비의료 정보만 kind `practice` 로 — 서로 다른 병원 3곳 이상 인용(`verify_practice`). 블로그엔 링크 없이 `[clinic websites]`, 출처 목록·JSON-LD 에 병원 주소 없음, 텔레그램 검수엔 근거 주소 표시 + 👤 사람 확인. 원칙·이유는 기획서 12-3-1 (다른 계정 댓글 링크 금지 포함). Gemini 검색 연동의 리디렉션 주소는 실제 주소로 바꿔 저장(`web.source_url`).
- Claude CLI(`llm._claude_cli`): 프롬프트는 stdin, 저장소 밖 임시 폴더에서 `--tools ""`(추가 검색 단계만 WebSearch·WebFetch)·`--strict-mcp-config`·`--no-session-persistence` 로 실행. 사용량 한도·시간 초과는 `llm.RateLimited` → 워커가 요청을 버리지 않고 다음 실행 때 재시도.
- 텔레그램 검수 메시지(`03_review.list_message`): 초안별 대본 전문·블로그 구성·걸린 항목 + `MEDIA:` 첨부(`review_doc` 가 만든 `<draft_id>.md`). PC 경로는 보내지 않는다. 게시 전 확인(rendered)에는 사진 넘기기형 미리보기 `carousel/preview.jpg` 첨부.
- 금지 표현은 `config/banned_terms.txt`, 병원명·연락처·체험담 등 패턴은 `02b_auto_review.py` 의 `RULE_PATTERNS`. 화장품 글(`content_type: skincare`)은 `COSMETIC_PATTERNS`(화장품법 §13 의약품 오인 표현)도 검사.
- 스폰서 글 끝에는 병원 공식 사이트 링크가 코드로 항상 붙는다(`sponsors.official_link_line`) → 블로그 빌드 시 추적 링크로 치환.
- 스폰서 트랙(기획서 12-1-1): 광고주 병원 = 광고 주체, 우리는 매체+제작 대행, 정액만. `config/sponsors.yaml`(git 제외). 스폰서 글은 `_sponsored_rules.md` 로 생성하고 광고 표시를 코드로 넣는다(`02_draft.add_disclosures`). 02b 는 광고 표시·계약·심의번호를 ⛔ 로 검사하고, 중립 글에 스폰서 병원이 나오면 ⛔. 03_review 는 현재 내용 기준 `병원확인` 없이는 승인하지 않는다 (--confirm 으로도 불가). 이 분리를 약화하는 변경은 하지 않는다.
- 의료·시술 정보 작성 기준 (2026-10-06, `write_draft.md`): 출처 문구에 최대한 가깝게(용어·수치·단서 유지), 출처가 그렇게까지 말하지 않으면 빼거나 톤을 낮춘 일반 표현 + 의사 확인. 사후관리 등 권고는 명령·허용형이 아니라 권고형. 의료 문장마다 바로 뒤에 [F#]. 여행 정보는 요약 허용.
- 블로그(`pipeline/site.py`): approved 이후 상태의 글만 게시(블로그는 초안 승인 = 게시 승인, 06_publish 의 ready_to_publish 제약은 영상용). LLM 출력의 원시 HTML·javascript: 링크 차단, [F#] → 의료 정보(kind 가 travel 이 아닌 사실)는 문장 옆 `[n · 도메인]` 이 출처 페이지로 바로 연결, 여행 정보는 각주 → 글 아래 출처 목록, JSON-LD·llms.txt·sitemap. 스폰서 글은 광고 배지 + 병원 링크 rel=sponsored + 추적 링크.
- 코스 주변 병원 목록(`pipeline/clinics.py`): 중립 여행 글 하단에 심평원 공공데이터 기준 관광지 반경 내 피부과 진료 의료기관을 **전부, 거리순으로** 코드가 붙인다 (LLM 본문엔 여전히 병원명 금지). 선택·추천·순위·후기·가격 없음, 스폰서는 같은 자리에 "Advertiser" 표시만. 병원 사이트 링크는 공공데이터에 주소가 있는 병원 **전부에 똑같이** (2026-10-06, `clinics.website`) — https 사이트는 병원별 추적 링크(`_clinic:<draft_id>:<병원 id>`, kind `clinic`), 리포트·CSV·영업 자료에는 병원명 없이 글·지역·시술 단위 합산만 (`analytics`). 병원별 수치를 해당 병원 영업에 쓰는 기능은 변호사 확인 전까지 만들지 않는다. 돈으로 목록 포함·제외·순서를 바꾸는 기능은 만들지 않는다. "가장 적합한 병원" 추천·매칭은 여전히 금지 (선택이 들어가면 §56 광고·알선 시비). 병원 한 곳을 앞세운 코스는 그 병원의 광고(스폰서 `--course`)로만.
- 코스 광고 카드(`site.route_ad_sponsor`/`route_ad_html`): sponsors.yaml `route_ad`(병원 확인 날짜 `confirmed` 필수, tagline 금지 표현 검사) + 같은 권역 중립 여행 글 하단에 작은 카드 1개, "Ad · Sponsored" 표시는 항상 분명히, 한국 방문자에게는 숨김(`site.yaml route_ads.hide_in_countries`, 해외 대상 광고). 본문·병원 목록 순서에는 영향 없음. 정액 계약만. **글 하나 = 광고주 하나**(독점 지면): 배정은 `content/site/route_ads.json` 에 고정돼 계약 기간 동안 안 바뀌고, 새 글은 배정이 적은 광고주에게. `route_ad.exclusive: true` = 권역 독점(같은 권역·겹치는 기간의 다른 코스 광고주가 있으면 sponsors 로드 오류).
- 여행 글 목록: `python -m pipeline.clinics refresh|show <관광지>` (`DATA_GO_KR_KEY`, 캐시 `content/clinics/`, 기준 `config/site.yaml` clinic_directory)
- **시스템의 선 (기획서 12-3)**: 이 시스템의 일은 독자를 병원 공식 사이트에 도착시키는 것까지. 상담 연결·예약 대행·환자 알선·전환(예약·결제) 추적은 만들지 않는다 (유치사업자 등록 후 별도 사업).
- 무상 파일럿: `contract.type: pilot` (최대 183일). 광고 표시·병원확인·트랙 분리는 유료와 동일, 문구만 "Partner content … (unpaid pilot)" (`sponsors.label`/`short_disclosure`).
- 추적 링크 메타: `content/site/tracked_links.json` — 키 = 스폰서 글 draft_id / 중립 글 `_registry:<draft_id>`, 값 = {url, code, kind, title, axis, keywords, sponsor_id, contract}. `pipeline/analytics.py` 가 이것과 `config/procedures.yaml` 로 시술별 집계.
- 링크 유입 추적기(`tracker/`, Cloudflare Worker + D1): 추적 링크 클릭 → 기록 → UTM 붙여 병원 사이트로 302. IP 원문 미저장. 스폰서 링크 대상은 공식 사이트(하위 도메인 포함)만. 유입 수치는 보고 자료일 뿐 요금은 정액.
- 테스트는 `llm.set_backend()` 로 가짜 LLM 을 쓰고 `SKIN_CONTENT_DIR`/`SKIN_LOG_DIR` 로 임시 폴더를 쓴다.

## 검수 요청 처리 (선택 기능 — 현재 미사용)
현재 검수는 Gemini 가 한다(작성이 Claude 라서). 아래는 검수 단계(`source_check`, `cross_review`)를 `provider: claude_code` 로 바꿨을 때의 절차다.
Hermes 의 `skin-worker` 크론(`pipeline/worker.py` → `02c_external_review.review_cycle`)이 같은 머신에서 `claude -p` 를 실행해 요청한다 (git 으로 주고받지 않음).
요청을 받으면:
1. 지정된 `review-queue/pending/<packet_id>.json` 을 읽는다. 파일 안 `instructions` 가 기준이다 (기획서 7장 규칙 + 출력 형식).
2. 초안마다 ② `sources[].page_excerpt` 로 사실별 supported/weak/unsupported 판정(페이지 본문만 근거, 외부 지식 금지), ③ 대본·블로그 교차 검수.
3. 결과를 `review-queue/done/<packet_id>.result.json` 한 파일에만 쓴다. 초안·코드·다른 파일은 건드리지 않고 커밋하지 않는다. 반영·정리는 Hermes 의 `import` 가 한다.
- 판정이 애매하면 weak/minor 로 두고 사람이 보게 한다.
