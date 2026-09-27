// 순수 함수 — 유입 채널 분류, 봇 판별, 이동 URL 생성 (테스트 대상)

export const SOURCES = ["tiktok", "instagram", "youtube", "blog", "ai", "search", "social", "direct", "other"];

// 링크에 직접 붙이는 ?s= 값 (채널별로 같은 링크를 나눠 쓰기 위함)
const PARAM_ALIASES = {
  tt: "tiktok", tiktok: "tiktok",
  ig: "instagram", insta: "instagram", instagram: "instagram",
  yt: "youtube", youtube: "youtube", shorts: "youtube",
  blog: "blog", web: "blog",
  ai: "ai",
};

// 리퍼러 호스트 → 채널. AI 검색 답변에서 링크를 눌러 들어온 경우를 따로 센다.
const HOST_RULES = [
  [/(^|\.)(chatgpt\.com|chat\.openai\.com|perplexity\.ai|gemini\.google\.com|bard\.google\.com|copilot\.microsoft\.com|claude\.ai|you\.com|phind\.com|meta\.ai)$/, "ai"],
  [/(^|\.)tiktok\.com$/, "tiktok"],
  [/(^|\.)instagram\.com$/, "instagram"],
  [/(^|\.)(youtube\.com|youtu\.be)$/, "youtube"],
  [/(^|\.)(google\.[a-z.]+|bing\.com|duckduckgo\.com|search\.yahoo\.com|yahoo\.co\.jp|naver\.com|baidu\.com|ecosia\.org|search\.brave\.com)$/, "search"],
  [/(^|\.)(facebook\.com|fb\.com|t\.co|x\.com|twitter\.com|reddit\.com|lnkd\.in|linkedin\.com|threads\.net|pinterest\.com)$/, "social"],
];

// 리퍼러를 안 보내는 앱 내 브라우저
const UA_RULES = [
  [/musical_ly|BytedanceWebview|TikTok/i, "tiktok"],
  [/Instagram/i, "instagram"],
  [/FBAN|FBAV/i, "social"],
];

const BOT_UA = /bot\b|bot\/|crawl|spider|slurp|facebookexternalhit|Twitterbot|Slackbot|WhatsApp|TelegramBot|Discordbot|preview|curl\/|wget|python-requests|python-urllib|Go-http-client|HeadlessChrome|GPTBot|ClaudeBot|PerplexityBot|Google-Extended|Bytespider|Applebot/i;

export function hostOf(url) {
  try {
    return new URL(url).hostname.toLowerCase().replace(/^www\./, "");
  } catch {
    return "";
  }
}

export function classifySource({ param, referrer, userAgent, siteHost }) {
  const p = (param || "").toLowerCase();
  if (PARAM_ALIASES[p]) return PARAM_ALIASES[p];
  const host = hostOf(referrer || "");
  if (host) {
    if (siteHost && (host === siteHost || host.endsWith("." + siteHost))) return "blog";
    for (const [re, source] of HOST_RULES) if (re.test(host)) return source;
    return "other";
  }
  for (const [re, source] of UA_RULES) if (re.test(userAgent || "")) return source;
  return "direct";
}

export function isBot(userAgent) {
  return !userAgent || BOT_UA.test(userAgent);
}

// 병원 사이트로 보낼 때 UTM 을 붙인다 → 병원이 자기 애널리틱스에서 우리 유입을 직접 확인 가능
export function buildTarget(targetUrl, { brand, source, campaign }) {
  const url = new URL(targetUrl);
  const set = (k, v) => { if (v && !url.searchParams.has(k)) url.searchParams.set(k, v); };
  set("utm_source", brand);
  set("utm_medium", source);
  set("utm_campaign", campaign);
  return url.toString();
}

const ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"; // 헷갈리는 문자(l,1,o,0) 제외
export function makeCode(length = 6) {
  const bytes = crypto.getRandomValues(new Uint8Array(length));
  return Array.from(bytes, (b) => ALPHABET[b % ALPHABET.length]).join("");
}

export function slug(text) {
  return (text || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40);
}

export async function visitorHash(ip, userAgent, day, salt) {
  const data = new TextEncoder().encode(`${salt}|${day}|${ip}|${userAgent}`);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest).slice(0, 8), (b) => b.toString(16).padStart(2, "0")).join("");
}

export function isValidTarget(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" && !!u.hostname && !u.username && !u.password;
  } catch {
    return false;
  }
}

export function isValidDay(value) {
  return /^\d{4}-\d{2}-\d{2}$/.test(value || "") && !Number.isNaN(Date.parse(value));
}
