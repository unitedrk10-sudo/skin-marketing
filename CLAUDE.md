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
