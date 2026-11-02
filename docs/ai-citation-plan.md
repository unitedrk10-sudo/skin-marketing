# AI 인용 높이기 실행 계획

작성 2026-11-02 · 상태: 승인, 진행 중

## 1. 목표와 현재 위치

**목표:** 해외 독자가 한국 피부 시술과 여행을 AI에 물었을 때, AI가 답변 근거로 Skinbound를 참고하게 만든다.

| 지표 | 현재 | 비고 |
|---|---|---|
| Gemini 인용 점검 | 0/20 | 11/1 점검의 16개 질문은 출처 기록이 없어 **실제 측정은 4개뿐** |
| 답변 중 실시간 글 읽기 (ChatGPT-User 등) | 4주간 0~1건 | Cloudflare 표본 |
| ChatGPT 검색 봇 | robots·sitemap만 읽음 | 글을 읽은 기록 없음 (표본) |
| 글 도달 (봇 패턴 제외) | 주 74 | 사람 수의 상한 |
| 외부 언급 | 자체 SNS 외 확인된 것 없음 | Threads 팔로워 0 |

이미 갖춘 것: 질문형 제목, FAQ, 구조화 데이터, AI 봇 허용, llms.txt, IndexNow. 이 영역은 더 투자하지 않는다.

## 2. 지키는 선

- **중립 글·Reddit 답변·데이터 세트에 병원명, 병원 추천, 병원별 순위를 넣지 않는다.** 데이터 세트는 지역 단위 합산만 공개한다.
- **Reddit은 사람이 직접 올린다.** API 게시, 봇, 웹 자동 조작, 다른 계정인 척하기, 대가성 후기는 만들지 않는다.
- **모든 Reddit 답변에 운영자임을 밝힌다.** 코드가 항상 붙인다.
- **의료 내용은 검수를 거친 사실 목록만 쓴다.** 쓰기는 Claude, 검수는 Gemini로 분리를 유지한다.
- **이미 게시된 글은 운영자 승인 없이 고치지 않는다.**

## 3. 실행 순서

### 1단계. 인용 점검 수정 (개발, 약 1일)

**확인한 원인:** 11/2에 출처가 비었던 질문 2개를 다시 돌렸더니 답변은 나왔지만 출처 주소가 0개였다. Gemini 검색이 돌지 않았거나 출처 정보가 오지 않은 것으로, 둘 중 어느 쪽인지는 수정 때 확인한다.

- 질문에 검색 사용을 지시하고, 출처가 없으면 한 번 재시도한다.
- 결과를 **인용됨 / 인용 안 됨 / 측정 실패**로 나눈다. 리포트는 "측정된 N개 중 M개"로 표시한다.
- 실행 뒤 Gemini 비용을 확인한다.
- 11/1 이전 결과는 비교 기준에서 뺀다.

**완료 기준:** 20개 중 측정 실패 2개 이하. 이 결과를 새 기준선으로 삼는다.

### 2단계. 구글·Bing 검색 현황 연결 (코드 완료 2026-11-02, 운영자 설정 대기)

- **운영자:** Search Console 읽기 권한 연결, Bing Webmaster 등록 여부 확인과 API 키 발급.
- **개발:** `pipeline/search_console.py`로 글별 노출·클릭·평균 순위·검색어를 가져와 주간 리포트에 추가한다.
- 색인되지 않은 글은 원인을 확인해 고친다.

**완료 기준:** 주간 리포트에서 글별 색인 여부와 상위 검색어가 보인다.

### 3단계. Reddit 답변 도구 (개발 완료 2026-11-02, 게시는 규칙 확인 뒤)

**자동 게시는 하지 않는다.** 2025년 11월부터 Reddit API는 사전 승인제이고 봇·AI 에이전트도 대상이다. 웹 자동 조작도 만들지 않는다. 댓글에는 X 같은 작성 링크도 없어 글 주소와 붙여넣을 본문을 메시지 2개로 보낸다.

흐름:

1. **후보 찾기:** Gemini 검색으로 주 1회 최근 질문 글을 찾거나, 운영자가 텔레그램에 질문을 붙여 넣는다.
2. **걸러내기:** 허용 목록 밖 서브레딧, 오래된 글, 병원 추천 요청 글은 제외한다.
3. **초안 작성:** Claude(`reddit_answer`)가 게시된 글의 사실 목록으로만 쓴다.
4. **검사:** 02b 금지 규칙을 코드로 검사하고, Gemini(`reddit_review`)가 사실과 과장 여부를 대조한다.
5. **운영자 확인:** 텔레그램으로 질문 원문, 답변 초안, 근거를 보낸다.
6. **게시:** 운영자가 자기 말투로 고쳐 직접 올리고 `레딧 완료 <URL>`로 기록한다.

답변 규칙:

- 링크 없이도 질문에 답이 되어야 한다. 링크는 서브레딧 규칙이 허용하고 글이 질문에 직접 답할 때만 하나 넣는다.
- 끝에 운영자 공개 문구를 넣는다. 예: `Disclosure: I run Skinbound, an information site about skin treatments in Korea.`
- 쓰지 않는 것: 병원명, 가격 권유, 체험담, "추천합니다" 같은 표현.
- "어느 병원이 좋아요?" 글에는 답하지 않는다. 의료법 §56 광고와 환자 알선 시비를 피하기 위해서다.
- 스폰서 글은 근거로 쓰지 않는다.
- 일주일에 최대 2개로 시작한다.

**게시 전 운영자가 할 일:**

- 실제 계정 하나를 정하고, 평소 활동을 먼저 쌓는다. 홍보 비중을 10분의 1 이하로 두라는 기준이 널리 인용되므로, 일반 참여가 훨씬 많아야 한다.
- Reddit 공식 규칙과 서브레딧별 규칙을 직접 읽는다. 이번 조사에서는 공식 페이지 접속이 막혀 **2차 자료로만 확인했다.**
- 서브레딧별로 홍보 규칙과 AI 글 허용 여부를 `config/reddit.yaml`에 확인 날짜와 함께 기록한다. 확인되지 않은 곳에는 초안을 만들지 않는다.

| 서브레딧 | 지금 아는 것 | 초기 판단 |
|---|---|---|
| r/PlasticSurgery | 2024년 말 미러 기준, 홍보·광고는 밴 사유 | 제외 |
| r/SeoulPlasticSurgery | 규칙 미확인, 회원 약 1.3만 | 규칙 확인 후 결정 |
| r/KoreaTravelBeauty | 규칙 미확인, 회원 약 4천 | 규칙 확인 후 결정 |
| r/koreatravel, r/SkincareAddiction, r/AsianBeauty | 규칙 미확인 | 규칙 확인 후 결정 |

**완료 기준:** 초안 5개를 운영자가 읽고 쓸 만하다고 판단한다. 이때까지는 게시하지 않는다.

### 4단계. 여행 × 시술 질문으로 주제 이동 (설정 변경, 반나절)

"Rejuran이란?" 같은 일반 의료 질문은 NIH, Mayo Clinic 같은 기관 출처와 겹친다. 시술 후 비행, 며칠 일정, 시술과 관광 순서처럼 기관이 잘 다루지 않는 질문에 집중한다.

- `topic_quota`를 **시술 정보 2 + 여행 × 시술 3 + 올리브영 화장품 추천 1**로 바꾼다 (운영자 결정).
- 화장품 추천은 선정 기준을 먼저 밝히고 여러 제품을 무상 편집 예시로 든다. 순위, "최고" 표현, 판매 링크는 없고 화장품법 §13 검사를 그대로 받는다.
- 인용 점검에서 권위 있는 출처가 적게 나온 질문을 목요일 주제 조사에 넘긴다.

**완료 기준:** 다음 주제 조사 후보에 인용 점검의 빈틈이 반영된다.

### 5단계. 영어 공개 데이터 세트 (D1·D2 공개 2026-11-02, 공공누리 제1유형 기준으로 운영자 결정)

**주소:** `skinboundkorea.com/data/`. 표, CSV 내려받기, 산출 방법, 갱신 기록, `Dataset` 구조화 데이터를 함께 제공한다.

| 세트 | 내용 | 원자료 | 상태 |
|---|---|---|---|
| D1. 지역별 피부과 진료기관 수 | 시·도와 시·군·구별 기관 수, 종별 구성, 영문 지역명 | 심평원 병원정보서비스 | 조회 확인: 전국 17,871곳, 서울 5,017곳 |
| D2. 관광지 주변 기관 수 | 주요 관광지 반경 700m 안의 기관 수 | D1 + `config/attractions.yaml` | 관광지 17곳 캐시 있음 |
| D3. 외국인 환자 추이 | 연도별 피부과 환자 수, 비중, 국적 | KHIDI 통계 보고서 | 원문·이용 조건 확인 필요 |
| D4. 비급여 가격 집계 | 지역별 공개 가격 범위 | 심평원 비급여 정보 | 보류. 법률 검토 전에는 만들지 않음 |

**반드시 표기할 한계 (D1):**

- 숫자는 "피부과 **진료과목이 있는** 기관"이며 **피부과 전문의 병원 수가 아니다.** 필터 정확성과 전문의 수 조회 가능 여부는 개발 때 검증한다.
- 기준일을 표시하고, 분기마다 갱신한다.

**이용 조건:**

- 심평원 병원정보서비스 페이지에는 "제3자 권리 포함: 저작권 표시, 공공저작물: 출처표시 (제1유형)"로 적혀 있다. 공공누리 제1유형은 일반적으로 출처 표시 조건으로 상업적 이용을 허용하지만, 데이터 페이지에 상업적 이용이 따로 명시되지는 않았다. **공개 전 심평원에 확인한다.**
- D3는 언론 보도 수치를 쓰지 않고 KHIDI 원문으로 확인한다. 참고로 보도된 2024년 피부과 외국인 환자는 705,044명, 비중 56.6%다.

**병원 데이터 처리:** 공개 파일에 병원명, 주소, 전화, 사이트, 좌표를 넣지 않는다. 표본이 너무 적은 지역은 묶어서 표시할지 개발 때 정한다.

**알리는 방법:** 공개 뒤 IndexNow 전송, 기존 글에서 내부 링크, Reddit 답변의 근거로 활용. 데이터 세트 등록처 제출은 공개 후 검토한다.

**완료 기준:** D1·D2 공개, CSV 내려받기 작동, 산출 방법 페이지 공개, 병원 정보 미포함을 테스트로 강제.

### 6단계. 성과 있는 글 갱신 (개발 중단. 12월에 유의미한 효과가 잡힌 뒤 재개)

- 노출은 있는데 순위나 클릭이 낮은 글, 출처가 오래된 글을 주간 리포트에 후보로 올린다.
- 고친 글은 초안과 같은 검수를 거치고, **운영자가 승인한 글만** 게시한다.
- 내용이 실제로 바뀔 때만 수정일을 바꾼다.

## 4. 일정

| 시기 | 작업 | 담당 |
|---|---|---|
| 11/2~11/3 | 1단계 수정, 새 기준선 | 개발 |
| 11/3~11/5 | 4단계 주제 비중 변경 (11/5 목요일 주제 조사 전) | 개발 |
| 11/4~11/9 | 2단계 연결 | 운영자 설정 후 개발 |
| 11/5~11/12 | 3단계 Reddit 도구, 초안 5개 | 개발 |
| 11/5부터 | Reddit 계정 직접 생성, 활동, 서브레딧 규칙 확인 | 운영자 |
| 11/10~11/20 | 5단계 D1·D2 | 심평원 확인 후 개발 |
| 11/29 | 8주 신뢰 단계 점검과 함께 인용 점검 | 자동 |
| 12월 | D3 개발. 6단계는 효과가 잡혔을 때만 | 개발 |

4단계는 11/5 주제 조사에 맞추려고 순서를 앞당겼다. 나머지 일정은 Claude 사용량과 운영자 설정 시점에 따라 밀릴 수 있다.

## 색인 기준선 (2026-11-02)

게시 글 11편 기준. 구글과 Bing은 색인이 따로라 각각 볼 수 있다. 다만 대조군 없이 모두 요청했으므로 자연 색인과 구분할 수 없다.

| 검색엔진 | 조치 | 시작 시점 색인 |
|---|---|---|
| 구글 | 미색인 7편 수동 색인 요청 (운영자) | 4/11 |
| Bing | 11편 URL 제출 | 1페이지 |

**판정 기준 (운영자 결정):** 구글 주간 노출 기울기의 변화로 본다. 조치 전 3주(10/12~11/1)와 후 3주(11/2~11/22)를 비교하고, 요청 7편과 이미 색인돼 있던 4편을 비교군으로 나눈다. 구글 숫자가 늦게 들어오므로 **11/29 주간 리포트에서 판정**한다. 조치 후 3주 노출이 30 미만이면 판정 불가로 둔다. 효과가 있어도 Reddit, 데이터 페이지, 시간 경과가 섞이므로 단정하지 않는다.

구글 요청 대상: how-many-days-korea-skin-treatment-trip, flying-after-skin-treatment-korea-timing-guide, juvelook-pdlla-collagen-booster-explained, thermage-korea-monopolar-rf-vs-hifu, rejuran-korea-downtime-sessions-aftercare, how-to-read-korean-cosmetic-labels-aftercare, jeju-busan-after-skin-treatment-sun-sea-sauna. 이미 색인돼 있던 4편: gangnam-sinsa-apgujeong-area-guide, bukchon-hanok-village-walk-after-skin-booster-laser-downtime, gyeongbokgung-hanbok-day-skin-treatment-order, laser-treatment-korea-sun-aftercare-autumn-seoul.

## 5. 성과 판단

| 지표 | 주기 | 12/28까지 볼 신호 |
|---|---|---|
| ChatGPT 검색 봇의 글 읽기 | 주간 | 0 → 글 읽기 시작 |
| 실시간 글 읽기 | 주간 | 매주 1건 이상 |
| Gemini 인용 (측정된 질문 기준) | 4주 | 1개 이상 |
| ChatGPT·Perplexity 수동 확인 (질문 5개) | 4주, 운영자 | 1회라도 인용 |
| 구글·Bing 노출 | 주간 | 증가 추세, 상위 검색어 확인 |
| Reddit 답변 | 주간 | 삭제되지 않음, 답글·추천 발생 |
| 데이터 페이지 | 주간 | 외부 링크나 AI 읽기 발생 |

이 값은 목표치가 아니라 판단 신호다. 근거가 되는 조사가 대부분 업체 분석이고 서로 어긋나는 결과도 있어서 효과를 수치로 약속할 수 없다.

**12/28 판단:**

- **신호가 있으면:** 효과가 나온 쪽에 작업을 늘린다.
- **봇은 오는데 인용이 0이면:** 내용 경쟁력 문제로 보고 주제와 데이터 세트를 강화한다.
- **봇이 글을 안 읽으면:** 색인과 외부 링크 문제부터 다시 본다.
- **Reddit 답변이 삭제되거나 경고를 받으면:** 해당 서브레딧 활동을 바로 멈추고 방식을 재검토한다.

## 6. 하지 않을 것

- llms.txt, FAQ 스키마 추가 작업: 뚜렷한 효과 근거가 없다.
- AI별 맞춤 최적화: 트래픽이 적어 측정이 어렵다.
- 날짜만 바꾼 갱신, 대량 Reddit 게시, 여러 계정 운영.
- 의사 검수 표기: 도움이 될 수 있으나 비용과 법률 검토가 필요하다. 수익이 나기 시작하면 다시 본다.

## 7. 운영자가 정할 일

| 항목 | 기본안 | 필요 시점 |
|---|---|---|
| 주제 비중 | 시술 2 + 여행 × 시술 3 + 화장품 1 (결정됨) | 적용 완료 |
| Reddit 계정 | 운영자 실제 계정 하나, 운영자임을 밝힘 | 11/5부터 |
| 데이터 세트 라이선스 | CC BY 4.0, 원자료 출처 표시 | D1 공개 전 |
| 심평원 상업적 이용 문의 | 운영자 또는 초안 대필 후 운영자 발송 | D1 공개 전 |

## 8. 근거 자료

- [How AI Engines Cite the Web (2026 연구 정리)](https://everything-pr.com/how-ai-engines-cite-the-web-the-six-studies-that-define-the-2026-evidence-base)
- [Does ranking predict AI citations? (Rankability)](https://www.rankability.com/blog/does-ranking-predict-ai-citations/)
- [Only 12% of AI-cited URLs rank in Google's top 10 (Ahrefs)](https://ahrefs.com/blog/ai-search-overlap/?faq=5)
- [87% of SearchGPT citations match Bing (Seer Interactive)](https://www.seerinteractive.com/insights/87-percent-of-searchgpt-citations-match-bings-top-results)
- [llms.txt shows no clear effect (Search Engine Journal)](https://www.searchenginejournal.com/llms-txt-shows-no-clear-effect-on-ai-citations-based-on-300k-domains/)
- [Google says AI optimization is just SEO (Practical Ecommerce)](https://www.practicalecommerce.com/google-says-ai-optimization-is-just-seo)
- [What sources does AI trust for health questions? (LLM Pulse)](https://llmpulse.ai/blog/ai-health-sources-analyzed/)
- [Reddit self-promotion rules 2026 (2차 자료)](https://redship.io/blog/reddit-self-promotion-rules-2026)
- [r/PlasticSurgery 규칙 미러 (2024년 말)](https://reddit.fsky.io/r/plasticsurgery)
- [심평원 병원정보서비스 (공공데이터포털)](https://www.data.go.kr/data/15001698/openapi.do)
- [피부과 외국인 환자 2024 (Korea JoongAng Daily)](https://www.koreajoongangdaily.com/lifestyle/k-beauty-rising-koreas-dermatology-clinics-see-117-fold-surge-in-foreign-patients-since-2009/12421818)
