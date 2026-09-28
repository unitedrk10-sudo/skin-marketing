# 채널·브랜드명 후보 (2026-09-28 사전 조사)

> 웹 검색으로 같은 이름의 사업·앱이 있는지만 확인한 1차 조사다. 도메인·상표·SNS 계정은 아래 "확정 전 확인" 절차로 직접 확인해야 한다 (이 환경에서는 도메인 조회 서버 접속이 막혀 있음).

## 기준
- 영어권·동남아 사용자가 **듣고 바로 철자를 칠 수 있는** 짧은 영어 이름
- **정보·가이드** 성격 (병원·의사로 오해될 이름 금지: Dr, Clinic, MD, Derm 단독 사용 X — 의료법 §56 자격 오인 방지)
- 최상급·비교 표현 금지 (Best, Top, No.1 — 7장 금지 표현과 충돌)
- 한국 + 피부 + 여행(방한 전 탐색) 연상, 트렌드 키워드 "glow-up trip" 과 연결 가능하면 가산점
- 경쟁 서비스와 혼동 없을 것 — 확인된 경쟁·유사: Unni(시술 비교·예약 앱), YeoTi/여신티켓(시술 앱), SkinSeoul(K-뷰티 쇼핑몰), Seoulistique Skin(클리닉)

## 후보
| 순위 | 이름 | 도메인 후보 | 의미·장점 | 확인 결과 / 주의 |
|---|---|---|---|---|
| 1 | **Skinbound** | skinbound.com / skinbound.co | "Korea-bound(한국행)" + skin. 방한 전 탐색층 정확히 겨냥, 짧고 철자 쉬움 | 같은 이름 사업·앱 검색 안 됨 |
| 2 | **Glowbound** | glowbound.com / glowbound.co | "glow-up trip" 트렌드 연결, 부드러운 인상 | 검색 안 됨. glow 는 뷰티 제품명에 흔해 상표 유사성 확인 필요 |
| 3 | **Seoul Skin Notes** | seoulskinnotes.com | "노트" = 정보·기록 성격이 분명, 신뢰감 | 검색 안 됨. 쇼핑몰 SkinSeoul 과 어순만 달라 혼동 가능 → 3순위 |
| 4 | Skin Trip Korea | skintripkorea.com | 뜻이 가장 직관적 (검색 키워드형) | 일반 단어 조합이라 상표 등록이 어렵고 브랜드력 약함 |
| 제외 | Derma Atlas / K-Derm Atlas | — | — | "Derm Atlas" 는 피부과 학술 이미지 아틀라스들이 사용 중 |

**추천: Skinbound** — 여행(시술 × 여행 축)과 피부를 한 단어로 묶고, 정보 채널 성격과도 맞다. 스폰서 트랙에서도 "Sponsored by ○○ on Skinbound" 처럼 매체명으로 자연스럽다.

## 확정 전 확인 (PC 또는 휴대폰에서 10분)
1. 도메인: Cloudflare 대시보드 → Domain Registration → Register Domains 에서 `.com` 우선 검색 (원가 판매, 연 약 $10)
2. 상표: 한국 [KIPRIS](https://www.kipris.or.kr) (3류 화장품, 35류 광고, 41류 교육·정보, 44류 의료·미용 서비스), 미국 [USPTO Trademark Search](https://tmsearch.uspto.gov)
3. 계정명: TikTok·Instagram·YouTube 핸들 `@skinbound` 등 선점 여부 (없으면 `@skinbound.kr`, `@skinboundkorea`)
4. 확정되면: `config/site.yaml` 의 `name`, `domain`, `tracker/wrangler.toml` 의 `BRAND`, `SITE_HOST` 변경
