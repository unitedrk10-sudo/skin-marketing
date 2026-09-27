-- 링크 유입 추적 (Cloudflare D1 = SQLite)
-- 적용: npx wrangler d1 execute skin-tracker --remote --file schema.sql

CREATE TABLE IF NOT EXISTS links (
  code        TEXT PRIMARY KEY,          -- 짧은 링크 코드
  target_url  TEXT NOT NULL,             -- 이동할 병원 사이트 (https)
  sponsor_id  TEXT,                      -- config/sponsors.yaml 의 id (없으면 일반 링크)
  label       TEXT,                      -- 어떤 글·캠페인용인지
  created_at  TEXT NOT NULL,
  active      INTEGER NOT NULL DEFAULT 1
);

-- 클릭 기록: IP 원문은 저장하지 않는다 (visitor = 날짜별 솔트 해시 → 하루 단위 고유 방문자 수만 셀 수 있음)
CREATE TABLE IF NOT EXISTS clicks (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  code          TEXT NOT NULL,
  ts            TEXT NOT NULL,           -- ISO 시각 (UTC)
  day           TEXT NOT NULL,           -- YYYY-MM-DD (UTC)
  source        TEXT NOT NULL,           -- tiktok|instagram|youtube|blog|ai|search|social|direct|other
  referrer_host TEXT,
  country       TEXT,
  is_bot        INTEGER NOT NULL,
  visitor       TEXT
);

CREATE INDEX IF NOT EXISTS idx_clicks_day ON clicks(day);
CREATE INDEX IF NOT EXISTS idx_clicks_code_day ON clicks(code, day);
