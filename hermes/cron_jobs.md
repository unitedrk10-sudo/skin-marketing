# Hermes 크론 작업 정의

작업 디렉터리는 저장소 루트. 모든 스크립트는 실패 시 0이 아닌 종료 코드 + stderr 원인 로그를 남긴다 → 0이 아니면 텔레그램 실패 알림.
환경변수: `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `LAW_API_OC` (저장소·로그에 남기지 않는다).

| 이름 | 스케줄 | 모드 | 명령 | 결과 처리 |
|---|---|---|---|---|
| weekly-topics | 월 09:00 | no-agent | `python -m pipeline.01_topics` | stdout 을 텔레그램으로 전송. 사람이 번호로 답장 → `draft-generate` 실행 |
| draft-generate | 주제 선택 답장 직후 | no-agent | `python -m pipeline.02_draft --week <주차> --pick <번호>` | 이어서 `auto-review` 실행 |
| auto-review | draft-generate 직후 | no-agent | `python -m pipeline.02b_auto_review --auto-regenerate` | 이어서 `review-request` 실행 |
| review-request | auto-review 직후 | no-agent | `python -m pipeline.03_review list` | stdout + 각 초안의 `script.md`, `blog.md` 첨부해 텔레그램 전송 |
| review-reply | 텔레그램 검수 답장 수신 시 | 에이전트 (review_handler 스킬) | `python -m pipeline.03_review apply "<정규화된 답장>"` | 종료 코드 2 → 스킬 규칙대로 다시 질문. 수정 요청이 있었으면 `review-request` 재실행 |
| preview-request | 수 (렌더링 후) | no-agent | `python -m pipeline.03_review list --stage rendered` | 영상 파일 첨부 전송 (`04_render_video` 구현 후) |
| preview-reply | 게시 확인 답장 수신 시 | 에이전트 | `python -m pipeline.03_review apply "<답장>" --stage rendered` | |
| law-sync | 월 08:00 | no-agent | `python -m pipeline.law_sync` | `legal/changes.md` 가 생기면 내용 전송 |

- `주차`는 `content/topics/` 에서 가장 최근 파일명(예: `2026-W40`).
- `02b --auto-regenerate`: ⛔ 차단 초안은 자동 재생성 1회 후 재검수. 그래도 ⛔ 면 그대로 사람 검수로 넘어간다.
- 게시·삭제(06_publish) 등 되돌릴 수 없는 작업은 명령 승인(approval) 대상으로 등록한다.
- 미구현: `04_render_video`, `05_preview`, `06_publish`, `07_report` (TTS·합성·예약 게시 도구 선정 후).
