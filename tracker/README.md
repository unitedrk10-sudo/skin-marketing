# 링크 유입 추적기 (Cloudflare Worker + D1)

우리 채널(숏폼 프로필·설명란, 블로그)에 거는 병원 링크를 짧은 추적 링크로 만들어,
병원 사이트로 넘어간 클릭을 채널·콘텐츠·국가별로 기록한다. 병원 영업·성과 보고 자료용 (기획서 12-1-1).

- 무료 한도 안에서 동작 (Workers 하루 10만 요청, D1 쓰기 하루 10만 행)
- IP 원문은 저장하지 않음. 하루 단위 고유 방문자만 셈 (날짜별 솔트 해시)
- 병원 사이트로 보낼 때 `utm_source/utm_medium/utm_campaign` 을 붙여 병원 GA 에서도 확인 가능
- 요금은 정액. 클릭·방문 수 비례 과금 금지 (의료법 §27③)

## 배포 (Hermes PC, 한 번)
```bash
cd tracker
npx wrangler login                              # Cloudflare 계정 로그인 (브라우저)
npx wrangler d1 create skin-tracker             # 출력된 database_id 를 wrangler.toml 에 입력
npx wrangler d1 execute skin-tracker --remote --file schema.sql
openssl rand -hex 24                            # 토큰 생성 → 아래 두 곳에 같은 값
npx wrangler secret put ADMIN_TOKEN
npx wrangler secret put VISITOR_SALT            # 아무 긴 임의 문자열
npx wrangler deploy                             # → https://skin-tracker.<계정>.workers.dev
```
저장소 `.env` 에:
```
TRACKER_URL=https://skin-tracker.<계정>.workers.dev
TRACKER_TOKEN=<ADMIN_TOKEN 과 같은 값>
```
도메인을 사면 Cloudflare 대시보드 → Workers → skin-tracker → Domains 에서 `go.<도메인>` 을 연결하고 TRACKER_URL 을 바꾼다.
블로그 도메인이 생기면 `wrangler.toml` 의 `SITE_HOST` 를 넣고 다시 배포 (블로그에서 온 클릭을 blog 로 분류). `BRAND` 는 브랜드명 확정 후 변경.

## 사용
```bash
python -m pipeline.tracker add --sponsor glow --label "Rejuran guide"   # 채널별 링크 4개 출력 (?s=tt|ig|yt|blog)
python -m pipeline.tracker list
python -m pipeline.tracker report --days 7                               # 주간 요약
python -m pipeline.tracker sponsor-report --sponsor glow --month 2026-10 # reports/sponsors/glow-2026-10.md
```
- 링크 대상은 스폰서의 공식 사이트(또는 하위 도메인)만 허용.
- **스폰서 병원 링크는 블로그 스폰서 글 안에만** 건다 (`add --sponsor` 는 `?s=blog` 링크만 출력). SNS 프로필·설명란에 대가 관계 병원 링크를 거는 것은 브랜디드 콘텐츠로 볼 여지가 있고, 틱톡은 미용 클리닉 브랜디드 콘텐츠를 금지한다. SNS 프로필에는 우리 블로그 주소를 건다 → 블로그를 거쳐 온 클릭은 리퍼러로 `blog` 로 분류된다.
- 스폰서가 아닌 일반 링크(`--target` 만)는 채널별(`?s=tt|ig|yt|blog`) 링크를 출력한다.
- AI 검색(ChatGPT·Perplexity·Gemini·Claude 등) 답변 안의 링크로 들어온 클릭은 리퍼러로 `ai` 로 분류된다.

## 개발
```bash
npm test          # node:sqlite 위에서 실제 SQL 로 Worker 검증 (의존성 없음)
npx wrangler dev  # 로컬 실행 (.dev.vars 에 ADMIN_TOKEN, VISITOR_SALT)
```
