# CLAUDE.md

기획서: `docs/derm-content-automation.md` (전체 설계·법규 체크리스트의 기준 문서)

## 작업 규칙
- 이 프로젝트는 **개발만** Claude Code로 하고, 운영은 Hermes가 한다. 스크립트는 사람 개입 없이 CLI로 단독 실행 가능해야 한다.
- 모든 LLM 호출은 `pipeline/llm.py`를 통해서만 한다. 모델명은 `config/models.yaml`에서 읽는다.
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
- 주간 주제: `python -m pipeline.01_topics`
- 초안 생성: `python -m pipeline.02_draft --week 2026-W40 --pick 1,3` / 수정: `--revise <draft_id> --note "..."`
- 자동 검수: `python -m pipeline.02b_auto_review --auto-regenerate`
- Claude Code 검수 한 사이클: `python -m pipeline.02c_external_review review` (export → claude -p → import)
- 워커(Hermes 크론): `python -m pipeline.worker run` / 주제 선택 요청: `python -m pipeline.worker request-drafts --pick 1,3`
- 사람 검수: `python -m pipeline.03_review list` / `python -m pipeline.03_review apply "1,3 승인"`
- Hermes 설치·운영: `hermes/README.md` (`bash hermes/install.sh`), 텔레그램 답장 스킬: `hermes/skills/skin-marketing/SKILL.md`

## 구조 메모
- 초안 = `content/<상태>/<draft_id>/` 폴더 (`draft.json`, `script.md`, `blog.md`, `review.json`, `history.json`). `content/` 는 운영 데이터라 git 에 올리지 않는다.
- 대본·블로그는 02_draft 의 사실 목록(`facts`, 사실마다 출처 URL)만 사용하고 `[F#]` / `fact_ids` 로 참조한다. 02b 는 이 참조를 기준으로 출처를 검증한다.
- 금지 표현은 `config/banned_terms.txt`, 병원명·연락처·체험담 등 패턴은 `02b_auto_review.py` 의 `RULE_PATTERNS`.
- 테스트는 `llm.set_backend()` 로 가짜 LLM 을 쓰고 `SKIN_CONTENT_DIR`/`SKIN_LOG_DIR` 로 임시 폴더를 쓴다.

## 검수 요청 처리 (Claude Code 가 운영 검수를 맡는 유일한 작업)
검수 단계(`source_check`, `cross_review`)는 `provider: claude_code` — Anthropic API 대신 Claude Code 가 한다.
Hermes 의 `skin-worker` 크론(`pipeline/worker.py` → `02c_external_review.review_cycle`)이 같은 머신에서 `claude -p` 를 실행해 요청한다 (git 으로 주고받지 않음).
요청을 받으면:
1. 지정된 `review-queue/pending/<packet_id>.json` 을 읽는다. 파일 안 `instructions` 가 기준이다 (기획서 7장 규칙 + 출력 형식).
2. 초안마다 ② `sources[].page_excerpt` 로 사실별 supported/weak/unsupported 판정(페이지 본문만 근거, 외부 지식 금지), ③ 대본·블로그 교차 검수.
3. 결과를 `review-queue/done/<packet_id>.result.json` 한 파일에만 쓴다. 초안·코드·다른 파일은 건드리지 않고 커밋하지 않는다. 반영·정리는 Hermes 의 `import` 가 한다.
- 판정이 애매하면 weak/minor 로 두고 사람이 보게 한다.
