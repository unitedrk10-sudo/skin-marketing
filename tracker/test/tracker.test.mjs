// node --test 로 실행. node:sqlite 위에 D1 과 같은 모양의 얇은 어댑터를 얹어 실제 SQL 을 검증한다.
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import worker from "../src/index.js";
import { buildTarget, classifySource, isBot } from "../src/logic.js";

function d1() {
  const db = new DatabaseSync(":memory:");
  db.exec(readFileSync(new URL("../schema.sql", import.meta.url), "utf8"));
  const stmt = (sql, args = []) => ({
    bind: (...a) => stmt(sql, a),
    first: async () => db.prepare(sql).get(...args) ?? null,
    all: async () => ({ results: db.prepare(sql).all(...args) }),
    run: async () => db.prepare(sql).run(...args),
  });
  return { prepare: (sql) => stmt(sql), raw: db };
}

let env;
const TOKEN = "t".repeat(32);
beforeEach(() => {
  env = { DB: d1(), ADMIN_TOKEN: TOKEN, VISITOR_SALT: "salt", BRAND: "skininfo", SITE_HOST: "skininfo.example" };
});

const call = (path, init = {}) => worker.fetch(new Request(`https://go.example${path}`, init), env, {});
const admin = (path, init = {}) =>
  call(path, { ...init, headers: { authorization: `Bearer ${TOKEN}`, "content-type": "application/json", ...(init.headers || {}) } });
const HUMAN = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1";

async function newLink(body = {}) {
  const res = await admin("/api/links", { method: "POST", body: JSON.stringify({ target_url: "https://clinic.example/rejuran", sponsor_id: "glow", label: "Rejuran guide", ...body }) });
  return res.json();
}

test("채널 분류: ?s= 우선, 리퍼러, 앱 내 브라우저 UA, 직접", () => {
  assert.equal(classifySource({ param: "tt" }), "tiktok");
  assert.equal(classifySource({ referrer: "https://chatgpt.com/c/abc" }), "ai");
  assert.equal(classifySource({ referrer: "https://www.perplexity.ai/search" }), "ai");
  assert.equal(classifySource({ referrer: "https://www.google.co.th/" }), "search");
  assert.equal(classifySource({ referrer: "https://skininfo.example/post", siteHost: "skininfo.example" }), "blog");
  assert.equal(classifySource({ userAgent: "Mozilla/5.0 ... Instagram 300.0" }), "instagram");
  assert.equal(classifySource({ userAgent: "Mozilla/5.0 ... musical_ly_2023" }), "tiktok");
  assert.equal(classifySource({ userAgent: HUMAN }), "direct");
  assert.equal(classifySource({ referrer: "https://random.example" }), "other");
});

test("봇 판별", () => {
  assert.ok(isBot("facebookexternalhit/1.1"));
  assert.ok(isBot("Mozilla/5.0 (compatible; GPTBot/1.0)"));
  assert.ok(isBot(""));
  assert.ok(!isBot(HUMAN));
});

test("UTM 부착 — 병원이 붙여둔 값은 덮지 않음", () => {
  const u = new URL(buildTarget("https://c.example/p?utm_source=keep&x=1", { brand: "b", source: "tiktok", campaign: "c" }));
  assert.equal(u.searchParams.get("utm_source"), "keep");
  assert.equal(u.searchParams.get("utm_medium"), "tiktok");
  assert.equal(u.searchParams.get("x"), "1");
});

test("관리 API 는 토큰 필요", async () => {
  assert.equal((await call("/api/links")).status, 401);
  assert.equal((await call("/api/links", { headers: { authorization: "Bearer wrong" } })).status, 401);
  assert.equal((await admin("/api/links")).status, 200);
});

test("https 가 아닌 대상은 거부 (오픈 리다이렉트 방지)", async () => {
  const res = await admin("/api/links", { method: "POST", body: JSON.stringify({ target_url: "javascript:alert(1)" }) });
  assert.equal(res.status, 400);
});

test("클릭 → 기록 + UTM 붙은 302, 통계 집계", async () => {
  const { code, url } = await newLink();
  assert.match(url, new RegExp(`/${code}$`));
  const hit = (qs, headers) => call(`/${code}${qs}`, { headers: { "user-agent": HUMAN, "cf-connecting-ip": "1.1.1.1", ...headers }, redirect: "manual" });

  const res = await hit("?s=tt");
  assert.equal(res.status, 302);
  const loc = new URL(res.headers.get("location"));
  assert.equal(loc.origin + loc.pathname, "https://clinic.example/rejuran");
  assert.equal(loc.searchParams.get("utm_source"), "skininfo");
  assert.equal(loc.searchParams.get("utm_medium"), "tiktok");
  assert.equal(loc.searchParams.get("utm_campaign"), "rejuran-guide");

  await hit("", { referer: "https://chatgpt.com/" });
  await hit("?s=tt");                                                    // 같은 사람 같은 날 → 고유 1
  await hit("?s=ig", { "cf-connecting-ip": "2.2.2.2" });
  await hit("", { "user-agent": "facebookexternalhit/1.1" });              // 봇 → 집계 제외
  await call(`/${code}`, { method: "HEAD", headers: { "user-agent": HUMAN } }); // HEAD → 기록 안 함

  const day = new Date().toISOString().slice(0, 10);
  const stats = await (await admin(`/api/stats?from=${day}&to=${day}&sponsor=glow`)).json();
  assert.deepEqual(stats.totals, { clicks: 4, unique_daily: 2, bots: 1 });
  const bySource = Object.fromEntries(stats.by_source.map((r) => [r.source, r.clicks]));
  assert.deepEqual(bySource, { tiktok: 2, ai: 1, instagram: 1 });
  assert.equal(stats.by_link[0].code, code);

  const other = await (await admin(`/api/stats?from=${day}&to=${day}&sponsor=nobody`)).json();
  assert.equal(other.totals.clicks, 0);
  const row = env.DB.raw.prepare("SELECT * FROM clicks LIMIT 1").get();
  assert.ok(!Object.values(row).includes("1.1.1.1"));                       // IP 원문 미저장
});

test("없는 코드·꺼진 링크는 404", async () => {
  assert.equal((await call("/zzzzzz")).status, 404);
  const { code } = await newLink();
  await admin(`/api/links/${code}/deactivate`, { method: "POST" });
  assert.equal((await call(`/${code}`, { headers: { "user-agent": HUMAN } })).status, 404);
});

test("통계 날짜 형식 검증", async () => {
  assert.equal((await admin("/api/stats?from=2026-1-1&to=x")).status, 400);
});

test("분석용 내보내기: 집계만, 방문자 해시 없음", async () => {
  const { code } = await newLink();
  const hit = (qs, ip) => call(`/${code}${qs}`, { headers: { "user-agent": HUMAN, "cf-connecting-ip": ip }, redirect: "manual" });
  await hit("?s=blog", "1.1.1.1");
  await hit("?s=blog", "2.2.2.2");
  await hit("?s=blog", "2.2.2.2");
  const day = new Date().toISOString().slice(0, 10);
  const res = await (await admin(`/api/export?from=${day}&to=${day}`)).json();
  assert.equal(res.rows.length, 1);
  assert.equal(res.rows[0].clicks, 3);
  assert.equal(res.rows[0].unique_visitors, 2);
  assert.equal(res.rows[0].source, "blog");
  assert.ok(!("visitor" in res.rows[0]));
  assert.equal((await call(`/api/export?from=${day}&to=${day}`)).status, 401);
});

// ---- 대기 명단 (2026-11-10)
const join = (fields, headers = {}) => call("/w", {
  method: "POST",
  headers: { origin: "https://skininfo.example", "user-agent": HUMAN, "cf-connecting-ip": "203.0.113.9", ...headers },
  body: new URLSearchParams({ email: "Reader@Example.com", agree: "yes", timing: "2027h1", page: "/rejuran-guide/", ...fields }),
});
const waitlist = async (qs = "") => (await admin(`/api/waitlist${qs}`)).json();

test("대기 명단: 가입 저장 → 감사 페이지로 303, 이메일은 소문자·중복은 한 줄", async () => {
  const res = await join({});
  assert.equal(res.status, 303);
  assert.equal(res.headers.get("location"), "https://skininfo.example/waitlist/thanks/");
  await join({ timing: "later" });
  const data = await waitlist("?emails=1");
  assert.equal(data.total, 1);
  assert.deepEqual(data.rows.map((r) => [r.email, r.timing, r.page]), [["reader@example.com", "later", "/rejuran-guide/"]]);
  assert.ok(!JSON.stringify(data).includes("203.0.113.9")); // IP 원문 미저장
  assert.equal((await waitlist()).rows, undefined); // 기본 응답엔 명단 없음 (집계만)
});

test("대기 명단: 다른 사이트의 양식·봇·숨김 칸·동의 없음·잘못된 주소는 저장하지 않는다", async () => {
  assert.equal((await join({}, { origin: "https://evil.example" })).status, 403);
  await join({ email: "bot@example.com" }, { "user-agent": "python-requests/2.32" });
  await join({ email: "trap@example.com", website: "http://spam" });
  const bad = await join({ email: "no-consent@example.com", agree: "" });
  assert.equal(bad.headers.get("location"), "https://skininfo.example/waitlist/?e=1");
  await join({ email: "not-an-email" });
  await join({ email: "x@example.com", page: "https://evil.example/", timing: "<script>" });
  const data = await waitlist("?emails=1");
  assert.deepEqual(data.rows.map((r) => [r.email, r.timing, r.page]), [["x@example.com", "unsure", ""]]);
});

test("대기 명단: 같은 방문자는 하루 5개까지, 관리 API 는 토큰 필요, 삭제 요청 처리", async () => {
  for (let i = 0; i < 8; i++) await join({ email: `a${i}@example.com` });
  assert.equal((await waitlist()).total, 5);
  assert.equal((await call("/api/waitlist")).status, 401);
  const del = await admin("/api/waitlist/delete", { method: "POST", body: JSON.stringify({ email: "A0@example.com" }) });
  assert.equal((await del.json()).deleted, 1);
  assert.equal((await waitlist()).total, 4);
});

// ---- 국가별 이동 주소 (2026-11-10 올리브영 US)
test("target_us: 미국 방문자만 다른 주소로, 리워드 코드 등 원래 쿼리는 그대로", async () => {
  const res = await admin("/api/links", { method: "POST", body: JSON.stringify({
    target_url: "https://global.oliveyoung.com/product/detail?prdtNo=GA1&rwardCode=SKINBOUND82&utm_source=influencers",
    target_us: "https://us.oliveyoung.com/", label: "oliveyoung: Lotion" }) });
  const { code } = await res.json();
  const go = (country) => worker.fetch(Object.assign(new Request(`https://go.example/${code}?s=blog`, { headers: { "user-agent": HUMAN } }),
    { cf: { country } }), env, {});
  const us = (await go("US")).headers.get("location");
  const sg = (await go("SG")).headers.get("location");
  assert.ok(us.startsWith("https://us.oliveyoung.com/?utm_source="));
  assert.ok(sg.startsWith("https://global.oliveyoung.com/product/detail?prdtNo=GA1&rwardCode=SKINBOUND82&utm_source=influencers&utm_medium=blog"));
  const bad = await admin("/api/links", { method: "POST", body: JSON.stringify({ target_url: "https://a.example/", target_us: "http://b.example/" }) });
  assert.equal(bad.status, 400);
  const sponsored = await admin("/api/links", { method: "POST", body: JSON.stringify({ target_url: "https://a.example/", sponsor_id: "glow", target_us: "https://b.example/" }) });
  assert.equal(sponsored.status, 400);
});
