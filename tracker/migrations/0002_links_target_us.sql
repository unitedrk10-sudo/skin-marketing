-- 2026-11-10: 미국 방문자 전용 이동 주소 (올리브영 글로벌은 미국 배송 안 함 → Olive Young US)
-- 이미 만든 DB 에 한 번만: npx wrangler d1 execute skin-tracker --remote --file migrations/0002_links_target_us.sql
-- (Worker 를 새로 배포하기 전에. 두 번 실행하면 duplicate column 오류가 나지만 데이터는 그대로다)
ALTER TABLE links ADD COLUMN target_us TEXT;
