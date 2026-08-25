// Password -> HMAC-signed cookie. No sessions table: the cookie carries its own
// expiry and a signature over it, so rotating COOKIE_SECRET kills every live
// session at once.

const enc = new TextEncoder();

function b64url(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function hmac(secret, msg) {
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  return b64url(new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(msg))));
}

// Constant-time compare. Both arguments are always HMAC outputs (fixed length,
// attacker-independent), never raw secrets -- so the early length return leaks
// nothing.
function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// Compare via HMAC rather than comparing the secrets themselves: equal-length
// digests keep the comparison constant-time regardless of input length.
export async function checkSecret(env, submitted, expected) {
  if (!expected) return false;
  const [a, b] = await Promise.all([
    hmac(env.COOKIE_SECRET, "cmp:" + submitted),
    hmac(env.COOKIE_SECRET, "cmp:" + expected),
  ]);
  return safeEqual(a, b);
}

export const COOKIE = "jl_session";

export async function mintCookie(env) {
  const days = Number(env.SESSION_DAYS || 60);
  const exp = Date.now() + days * 86400_000;
  const sig = await hmac(env.COOKIE_SECRET, "sess:" + exp);
  const value = `${exp}.${sig}`;
  const maxAge = days * 86400;
  return `${COOKIE}=${value}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=${maxAge}`;
}

export function clearCookie() {
  return `${COOKIE}=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0`;
}

export async function isAuthed(request, env) {
  const header = request.headers.get("Cookie") || "";
  const match = header.match(new RegExp("(?:^|;\s*)" + COOKIE + "=([^;]+)"));
  if (!match) return false;
  const [expRaw, sig] = match[1].split(".");
  const exp = Number(expRaw);
  if (!Number.isFinite(exp) || exp < Date.now()) return false;
  return safeEqual(sig || "", await hmac(env.COOKIE_SECRET, "sess:" + exp));
}

// --- login throttling ------------------------------------------------------

function clientIp(request) {
  return request.headers.get("CF-Connecting-IP") || "unknown";
}

export async function throttled(request, env) {
  const windowMin = Number(env.LOGIN_WINDOW_MIN || 15);
  const max = Number(env.MAX_LOGIN_FAILS || 8);
  const row = await env.DB.prepare(
    "SELECT fails, window_start FROM login_attempts WHERE ip = ?",
  ).bind(clientIp(request)).first();
  if (!row) return false;
  const age = Date.now() - Date.parse(row.window_start);
  if (age > windowMin * 60_000) return false; // window lapsed
  return row.fails >= max;
}

export async function noteFailure(request, env) {
  const windowMin = Number(env.LOGIN_WINDOW_MIN || 15);
  const ip = clientIp(request);
  const now = new Date().toISOString();
  const row = await env.DB.prepare(
    "SELECT fails, window_start FROM login_attempts WHERE ip = ?",
  ).bind(ip).first();
  const stale = !row || Date.now() - Date.parse(row.window_start) > windowMin * 60_000;
  if (stale) {
    await env.DB.prepare(
      "INSERT INTO login_attempts (ip, fails, window_start) VALUES (?, 1, ?)" +
      " ON CONFLICT(ip) DO UPDATE SET fails = 1, window_start = excluded.window_start",
    ).bind(ip, now).run();
  } else {
    await env.DB.prepare(
      "UPDATE login_attempts SET fails = fails + 1 WHERE ip = ?",
    ).bind(ip).run();
  }
}

export async function clearFailures(request, env) {
  await env.DB.prepare("DELETE FROM login_attempts WHERE ip = ?").bind(clientIp(request)).run();
}
