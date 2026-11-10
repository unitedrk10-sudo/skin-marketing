// 스폰서 병원 링크 유입 추적 — Cloudflare Worker + D1
//   GET  /<code>[?s=tt|ig|yt|blog]  → 클릭 기록 후 병원 사이트로 302 (UTM 부착)
//   /api/* (Authorization: Bearer <ADMIN_TOKEN>)
//     POST /api/links {target_url, sponsor_id?, label?}   링크 생성
//     GET  /api/links                                     링크 목록
//     POST /api/links/<code>/deactivate                   링크 끄기
//     GET  /api/stats?from=YYYY-MM-DD&to=YYYY-MM-DD[&sponsor=<id>][&code=<code>]
//     GET  /api/export?from=YYYY-MM-DD&to=YYYY-MM-DD   분석용 일·링크·채널·국가별 집계
//     GET  /api/waitlist[?emails=1]                      대기 명단 집계 (emails=1 이면 명단 포함)
//     POST /api/waitlist/delete {email}                  삭제 요청 처리
//   POST /w (블로그의 대기 명단 양식, form) → 저장 후 블로그의 /waitlist/thanks/ 로 303
// 개인정보: IP 원문 미저장. 날짜별 솔트 해시로 하루 단위 고유 방문자만 센다.

import {
  WAITLIST_TIMINGS, buildTarget, classifySource, cleanEmail, cleanPage, hostOf, isBot, isValidDay, isValidTarget,
  makeCode, slug, visitorHash,
} from "./logic.js";

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json; charset=utf-8" } });

function authorized(request, env) {
  const expected = env.ADMIN_TOKEN || "";
  const got = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
  if (!expected || got.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) diff |= expected.charCodeAt(i) ^ got.charCodeAt(i);
  return diff === 0;
}

async function recordClick(request, env, link, source) {
  const ua = request.headers.get("user-agent") || "";
  const referrer = request.headers.get("referer") || "";
  const now = new Date();
  const day = now.toISOString().slice(0, 10);
  const ip = request.headers.get("cf-connecting-ip") || "";
  const visitor = await visitorHash(ip, ua, day, env.VISITOR_SALT || "");
  let refHost = "";
  try { refHost = referrer ? new URL(referrer).hostname : ""; } catch { /* 잘못된 리퍼러 무시 */ }
  await env.DB.prepare(
    "INSERT INTO clicks (code, ts, day, source, referrer_host, country, is_bot, visitor) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
  ).bind(link.code, now.toISOString(), day, source, refHost, request.cf?.country || "", isBot(ua) ? 1 : 0, visitor).run();
}

async function redirect(request, env, ctx, code, url) {
  const link = await env.DB.prepare("SELECT * FROM links WHERE code = ? AND active = 1").bind(code).first();
  if (!link) return new Response("Not found", { status: 404 });
  const source = classifySource({
    param: url.searchParams.get("s"), referrer: request.headers.get("referer") || "",
    userAgent: request.headers.get("user-agent") || "", siteHost: env.SITE_HOST,
  });
  if (request.method === "GET") {
    // 기록 실패가 이동을 막지 않게 응답과 분리
    const task = recordClick(request, env, link, source).catch((e) => console.error("click log failed", e));
    if (ctx?.waitUntil) ctx.waitUntil(task); else await task;
  }
  const target = buildTarget(link.target_url, {
    brand: env.BRAND || "skin-info", source, campaign: slug(link.label) || link.code,
  });
  return new Response(null, { status: 302, headers: { location: target, "cache-control": "no-store", "referrer-policy": "no-referrer-when-downgrade" } });
}

async function createLink(request, env) {
  let body;
  try { body = await request.json(); } catch { return json({ error: "JSON body 필요" }, 400); }
  if (!isValidTarget(body.target_url)) return json({ error: "target_url 은 https 주소여야 합니다" }, 400);
  const sponsor = body.sponsor_id ? String(body.sponsor_id) : null;
  if (sponsor && !/^[a-z0-9][a-z0-9-]{1,39}$/.test(sponsor)) return json({ error: "sponsor_id 형식 오류" }, 400);
  for (let attempt = 0; attempt < 5; attempt++) {
    const code = makeCode();
    const exists = await env.DB.prepare("SELECT code FROM links WHERE code = ?").bind(code).first();
    if (exists) continue;
    await env.DB.prepare("INSERT INTO links (code, target_url, sponsor_id, label, created_at) VALUES (?, ?, ?, ?, ?)")
      .bind(code, body.target_url, sponsor, String(body.label || "").slice(0, 120), new Date().toISOString()).run();
    return json({ code, url: `${new URL(request.url).origin}/${code}` }, 201);
  }
  return json({ error: "코드 생성 실패" }, 500);
}

async function stats(env, url) {
  const from = url.searchParams.get("from");
  const to = url.searchParams.get("to");
  if (!isValidDay(from) || !isValidDay(to)) return json({ error: "from, to 는 YYYY-MM-DD" }, 400);
  const where = ["c.day >= ?", "c.day <= ?"];
  const args = [from, to];
  if (url.searchParams.get("sponsor")) { where.push("l.sponsor_id = ?"); args.push(url.searchParams.get("sponsor")); }
  if (url.searchParams.get("code")) { where.push("c.code = ?"); args.push(url.searchParams.get("code")); }
  const base = `FROM clicks c JOIN links l ON l.code = c.code WHERE ${where.join(" AND ")}`;
  const human = `${base} AND c.is_bot = 0`;
  const q = (sql) => env.DB.prepare(sql).bind(...args);
  const [totals, bots, bySource, byLink, byDay, byCountry] = await Promise.all([
    q(`SELECT COUNT(*) AS clicks, COUNT(DISTINCT c.day || c.visitor) AS unique_daily ${human}`).first(),
    q(`SELECT COUNT(*) AS bots ${base} AND c.is_bot = 1`).first(),
    q(`SELECT c.source, COUNT(*) AS clicks ${human} GROUP BY c.source ORDER BY clicks DESC`).all(),
    q(`SELECT c.code, l.label, l.sponsor_id, l.target_url, COUNT(*) AS clicks ${human} GROUP BY c.code ORDER BY clicks DESC`).all(),
    q(`SELECT c.day, COUNT(*) AS clicks ${human} GROUP BY c.day ORDER BY c.day`).all(),
    q(`SELECT c.country, COUNT(*) AS clicks ${human} GROUP BY c.country ORDER BY clicks DESC LIMIT 20`).all(),
  ]);
  return json({
    from, to, sponsor: url.searchParams.get("sponsor") || null,
    totals: { clicks: totals.clicks, unique_daily: totals.unique_daily, bots: bots.bots },
    by_source: bySource.results, by_link: byLink.results, by_day: byDay.results, by_country: byCountry.results,
  });
}

// 분석용 내보내기 — 일·링크·채널·국가별 집계 (개별 클릭·방문자 해시는 내보내지 않음)
async function exportRows(env, url) {
  const from = url.searchParams.get("from");
  const to = url.searchParams.get("to");
  if (!isValidDay(from) || !isValidDay(to)) return json({ error: "from, to 는 YYYY-MM-DD" }, 400);
  const { results } = await env.DB.prepare(
    `SELECT c.day, c.code, l.sponsor_id, l.label, l.target_url, c.source, c.country, c.is_bot,
            COUNT(*) AS clicks, COUNT(DISTINCT c.visitor) AS unique_visitors
     FROM clicks c JOIN links l ON l.code = c.code
     WHERE c.day >= ? AND c.day <= ?
     GROUP BY c.day, c.code, c.source, c.country, c.is_bot
     ORDER BY c.day, c.code`,
  ).bind(from, to).all();
  return json({ from, to, rows: results });
}

// 대기 명단 가입 — 블로그 양식에서만 받는다 (Origin/Referer 가 블로그 도메인). 봇·숨김 칸·동의 없음은 저장하지 않는다.
const WAITLIST_DAILY_LIMIT = 5; // 같은 방문자(하루 해시)가 하루에 넣을 수 있는 주소 수

async function joinWaitlist(request, env) {
  const site = env.SITE_HOST || "";
  const back = (path) => new Response(null, { status: 303, headers: { location: `https://${site}${path}`, "cache-control": "no-store" } });
  const from = hostOf(request.headers.get("origin") || request.headers.get("referer") || "");
  if (!site || !(from === site || from.endsWith("." + site))) return new Response("Forbidden", { status: 403 });
  let form;
  try { form = await request.formData(); } catch { return back("/waitlist/?e=1"); }
  const ua = request.headers.get("user-agent") || "";
  if (form.get("website") || isBot(ua)) return back("/waitlist/thanks/"); // 숨김 칸을 채운 봇: 저장하지 않고 조용히 끝
  const email = cleanEmail(form.get("email"));
  if (!email || form.get("agree") !== "yes") return back("/waitlist/?e=1");
  const timing = WAITLIST_TIMINGS.includes(form.get("timing")) ? form.get("timing") : "unsure";
  const now = new Date();
  const day = now.toISOString().slice(0, 10);
  const visitor = await visitorHash(request.headers.get("cf-connecting-ip") || "", ua, day, env.VISITOR_SALT || "");
  const today = await env.DB.prepare("SELECT COUNT(*) AS n FROM waitlist WHERE day = ? AND visitor = ?").bind(day, visitor).first();
  if ((today?.n || 0) >= WAITLIST_DAILY_LIMIT) return back("/waitlist/thanks/");
  const source = classifySource({ param: form.get("s"), referrer: "", userAgent: ua, siteHost: site });
  await env.DB.prepare(
    `INSERT INTO waitlist (email, created_at, day, timing, country, source, page, visitor) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(email) DO UPDATE SET timing = excluded.timing`,
  ).bind(email, now.toISOString(), day, timing, request.cf?.country || "", source, cleanPage(form.get("page")), visitor).run();
  return back("/waitlist/thanks/");
}

async function waitlistStats(env, url) {
  const q = (sql) => env.DB.prepare(sql);
  const [total, byDay, byCountry, byTiming, bySource] = await Promise.all([
    q("SELECT COUNT(*) AS n FROM waitlist").first(),
    q("SELECT day, COUNT(*) AS n FROM waitlist GROUP BY day ORDER BY day").all(),
    q("SELECT country, COUNT(*) AS n FROM waitlist GROUP BY country ORDER BY n DESC").all(),
    q("SELECT timing, COUNT(*) AS n FROM waitlist GROUP BY timing ORDER BY n DESC").all(),
    q("SELECT source, COUNT(*) AS n FROM waitlist GROUP BY source ORDER BY n DESC").all(),
  ]);
  const out = { total: total.n, by_day: byDay.results, by_country: byCountry.results, by_timing: byTiming.results, by_source: bySource.results };
  if (url.searchParams.get("emails") === "1") {
    out.rows = (await q("SELECT email, created_at, timing, country, source, page FROM waitlist ORDER BY created_at").all()).results;
  }
  return json(out);
}

async function api(request, env, url) {
  if (!authorized(request, env)) return json({ error: "unauthorized" }, 401);
  const path = url.pathname.replace(/\/+$/, "");
  if (path === "/api/links" && request.method === "POST") return createLink(request, env);
  if (path === "/api/links" && request.method === "GET") {
    const { results } = await env.DB.prepare("SELECT * FROM links ORDER BY created_at DESC").all();
    return json({ links: results });
  }
  const off = path.match(/^\/api\/links\/([a-z0-9]{4,12})\/deactivate$/);
  if (off && request.method === "POST") {
    await env.DB.prepare("UPDATE links SET active = 0 WHERE code = ?").bind(off[1]).run();
    return json({ code: off[1], active: false });
  }
  if (path === "/api/stats" && request.method === "GET") return stats(env, url);
  if (path === "/api/export" && request.method === "GET") return exportRows(env, url);
  if (path === "/api/waitlist" && request.method === "GET") return waitlistStats(env, url);
  if (path === "/api/waitlist/delete" && request.method === "POST") {
    let body;
    try { body = await request.json(); } catch { return json({ error: "JSON body 필요" }, 400); }
    const email = cleanEmail(body.email);
    if (!email) return json({ error: "email 형식 오류" }, 400);
    const res = await env.DB.prepare("DELETE FROM waitlist WHERE email = ?").bind(email).run();
    return json({ deleted: res.meta?.changes ?? res.changes ?? 0 });
  }
  return json({ error: "not found" }, 404);
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/")) return api(request, env, url);
    if (url.pathname === "/w" && request.method === "POST") return joinWaitlist(request, env);
    const code = url.pathname.slice(1);
    if (/^[a-z0-9]{4,12}$/.test(code) && (request.method === "GET" || request.method === "HEAD")) {
      return redirect(request, env, ctx, code, url);
    }
    if (url.pathname === "/robots.txt") return new Response("User-agent: *\nDisallow: /\n", { headers: { "content-type": "text/plain" } });
    return new Response("Not found", { status: 404 });
  },
};
