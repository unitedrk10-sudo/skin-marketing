# 숏폼(쇼츠·릴스) 운영 인수인계

작성 2026-11-04 · 새 세션의 시작 문서. 운영 규칙은 `CLAUDE.md`, 전체 설계는 `docs/derm-content-automation.md`, 채널 판단은 `docs/marketing-framework.md`를 먼저 읽는다.

## 새 세션이 할 일

**지금까지의 숏폼 실험·조사를 바탕으로 숏폼을 다시 시작할지, 어떤 형식·채널로 할지 정하고, 정한 기준에 맞춰 도구를 정비한다.**

1. **재개 여부와 시점:** 현재는 보류 상태다. 11/30 SNS 공유 실험 결과를 보고 판단하자는 것이 지난 세션의 권고였다.
2. **화면 방식:** 실험 E/BE/M 결과를 정리하고 형식 하나를 정한다.
3. **채널 순서와 계정:** 유튜브 → 인스타그램 → 틱톡 순서를 권고했다. 계정 개설과 게시 방식(수동 또는 API)을 정한다.
4. **대본 파이프라인 재가동:** `shorts_scripts.enabled`를 켤지, 주당 편수를 몇 편으로 할지 정한다.
5. **크리에이터 협업:** `docs/creator-outreach.md`의 목록으로 연락한다. 우리 숏폼과 별개로 진행해도 된다.

## 현재 상태 (2026-11-04)

| 항목 | 상태 | 설정 |
|---|---|---|
| 숏폼 렌더링 | **중단 (2026-10-10).** 수익이 나기 전까지 블로그·Threads·X만 운영 | `channels.yaml shortform.format: "off"` |
| 숏폼 대본 | **보류 (2026-11-01).** Gemini 검수 비용 때문에 워커가 미리 만들지 않음 | `channels.yaml shorts_scripts.enabled: false` |
| 화면 소스 실험 | 새 일정 없음. 워커는 클립 대기 중인 실험이 있는지 계속 확인 | `config/experiments.yaml`, `experiment.poll_message` |
| 데이터 그래프 SNS 게시 | **보류 (2026-11-03).** SNS는 가볍게 운영 | `docs/data-content-strategy.md` 6장 |
| 게시 채널 계정 | 유튜브·인스타그램·틱톡 모두 **미개설 또는 미확인.** `.env`에 관련 키가 없음 | — |

## 이미 있는 도구

| 단계 | 코드 | 명령 |
|---|---|---|
| 대본 (한 영상 = 한 질문, 20~30초, 위험·한계 줄 필수) | `pipeline/shorts.py`, `prompts/shorts_*.md` | `python -m pipeline.shorts plan\|list\|apply "1,3 승인"` |
| 영상 렌더링 (Gemini TTS Kore 1.1배 + 클립 + 자막) | `pipeline/04_render_video.py`, `pipeline/broll.py` | `python -m pipeline.04_render_video [--id <draft_id>]` |
| 사진 넘기기형 | `pipeline/carousel.py` | `--refresh <draft_id>` (보류: 시험 결과가 약했음) |
| 설명 애니메이션 (Remotion) | `pipeline/explainer.py`, `video/` | 실험 E·BE |
| 화면 소스 실험 | `pipeline/experiment.py` | `python -m pipeline.experiment today\|build\|score\|summary` |
| 게시 키트 (채널별 문구, 수동 게시 후 기록) | `pipeline/06_publish.py` | `kits\|status\|done <id> <채널> <URL>` |
| 블로그 설명 그림 (같은 Remotion 사용) | `pipeline/figures.py` | 운영 중. 숏폼과 별개 |

`06_publish.py`는 `ready_to_publish/`만 처리한다. 이 제약은 바꾸지 않는다.

## 실험 결과 (`python -m pipeline.experiment summary`)

| 방식 | 내용 | 결과 |
|---|---|---|
| A | 무료 영상 자동 (Pixabay+Pexels) | 2026-10-19 **0점 (모든 항목)** → 중단 |
| B | Google Flow 직접 생성 (Veo 3.1 Lite, 사람이 생성) | B1 점수 4·1·4·2. 화면 일관성은 좋고 내용 일치가 약함. 평균 9.0/15. 완성까지 831~2888분 |
| C | Flow + Aside | 클립 대기 상태에서 멈춤 |
| E | 코드로 그리는 설명 애니메이션 (Remotion) | E2 평가: 브랜드 8 · 전달 7 · 몰입 5 ("정적이다"). 완성까지 17~20분 |
| BE | Flow 클립 배경 + E 도식 | 완성. 점수 미기록 |
| M | 섞기: 원리·분위기는 Flow, 숫자·목록은 우리 도식 | 클립 대기 상태에서 멈춤 |

- 대상 글은 주로 W41-10(리쥬란)이다.
- 클립 대기 상태로 멈춘 실험 4개(C, 10-22 B, B-3d, M)는 정리하거나 이어서 완성해야 한다.
- Flow 웹 자동 조작은 시스템에 넣지 않는다. 클립은 사람이 만들어 `content/experiments/<id>/clips/`에 넣는다.

## 시장 조사 요약 (2026-11-03)

- **검색:** 미국 소비자의 49%, Z세대의 65%가 TikTok을 검색에 쓴다. 그러나 Google보다 TikTok을 선호하는 Z세대는 4%뿐이다.
- **AI 인용:** Google AI Overviews에서 가장 많이 인용되는 도메인은 유튜브(29.5%)다. 그중 94%가 긴 영상이고 쇼츠는 5.7%다.
- **K-뷰티 첫 접점:** SNS가 첫 접점이며, 후기(39.1%)와 인플루언서(38.5%) 비중이 크다.
- **TikTok:** 위험 안내가 없는 미용 시술 콘텐츠는 18세 이상 제한·추천 제외 대상이다. Branded Content Policy는 미용 클리닉·시술 홍보를 금지한다.
- **권고:** 재개한다면 유튜브를 먼저 하고(긴 영상도 검토), 인스타그램을 두 번째, 틱톡을 마지막에 한다.
- **스팸 위험:** 같은 틀의 영상을 대량으로 올리면 유튜브의 "대량 생산·반복 콘텐츠"(2026-07) 판정에 가까워진다. 적게, 내용을 다르게 만든다.
- **인플루언서가 되어야 하나:** 아니다. 시술 체험 영상은 의료광고 규정상 치료 경험담 위험이 있다. 우리 역할은 크리에이터와 AI가 인용하는 **검증된 참고 자료**다. 병목은 배포이므로, 크리에이터 협업이 숏폼 자체 운영보다 먼저일 수 있다.

## 지키는 선

- 얼굴, 시술 장면, 주사, 비포·애프터, "완벽한 피부" 결과를 보여 주지 않는다 (장면 프롬프트 금지 규칙).
- 병원명을 쓰지 않고, 시술·병원을 추천하지 않는다. 위험·한계 줄을 빼지 않는다.
- 사실은 승인된 블로그 글의 사실 목록에서만 가져온다. 쓰기는 Claude, 검수는 Gemini가 맡는다.
- 트렌드 사운드는 라이선스 문제로 쓰지 않는다. 배경 음악은 라이선스를 확인한 파일만 `content/library/music/`에 넣는다.
- 스폰서 글은 `channels.yaml sponsored_policy`에서 허용하기 전까지 숏폼에서 제외한다.
- 게시 자동화는 공식 API로만 한다. 웹 자동 조작이나 브라우저 게시는 만들지 않는다. API 실패 시 오류만 알린다.
- 숨긴 계정, 다른 사람인 척하는 계정, 대가성 후기는 만들지 않는다.

## 로컬 설정 점검 (2026-11-04)

**렌더링은 지금 PC에서 추가 설치 없이 가능하다.**

| 항목 | 상태 |
|---|---|
| ffmpeg·ffprobe (libass 자막 필터 포함) | ✅ 설치됨 |
| Node v22 + `video/node_modules` (Remotion) | ✅ 설치됨 (블로그 그림에도 사용 중) |
| `GEMINI_API_KEY` (TTS·검수) | ✅ |
| `PIXABAY_API_KEY` | ✅ (A 방식은 중단 상태) |
| `PEXELS_API_KEY` | ❌ 없음. A 방식을 다시 쓸 때만 필요 |
| 배경 음악 `content/library/music/` | ❌ 비어 있음. 선택 사항 |
| 직접 모은 클립 `content/library/videos/` | ❌ 없음. 선택 사항 |

**게시 방식에 따라 새로 필요한 것** (수동 게시면 모두 불필요):

| 채널 | 수동 게시 | API 자동 게시에 필요한 것 |
|---|---|---|
| 유튜브 | 계정·채널만 | Google Cloud OAuth 클라이언트 + YouTube Data API 업로드 권한. 토큰 파일은 저장소 밖에 둔다 |
| 인스타그램 | 계정만 | 비즈니스·크리에이터 계정 + Facebook 페이지 연결 + Meta 앱 `instagram_content_publish` 권한. 기존 Threads 앱과는 권한이 별도 |
| 틱톡 | 계정만 | Content Posting API 앱 심사. 심사 전에는 비공개로만 게시 가능 |

**권고:** 처음 몇 주는 `06_publish kits`로 키트를 받아 사람이 직접 올린다. 반응이 확인되면 유튜브부터 API를 붙인다. 계정 개설과 비밀번호 입력은 운영자가 직접 한다.

## 세션 운영 메모

- 같은 저장소(`main`)에서 작업한다. Reddit 세션도 같은 저장소에 푸시하므로 커밋 전에 `git pull --rebase --autostash origin main`을 한다.
- `legal/` 아래 커밋하지 않은 변경은 의도적으로 제외해 둔 것이다. 함께 커밋하지 않는다.
- 테스트는 `.venv\Scripts\python.exe -m pytest -q`로 돌린다. 시스템 파이썬에는 matplotlib가 없다.
- 운영자에게 먼저 텔레그램 보고를 보내지 않는다. 운영 메시지는 그대로 둔다.
- 앞으로 볼 날짜:
  - 11/9: 주간 리포트
  - 11/29: 색인 기울기 판정
  - **11/30: SNS 공유 실험 종료 (숏폼 재개 판단 근거)**
  - 12/28: 계획 판단
