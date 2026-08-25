import {
  checkSecret, mintCookie, clearCookie, isAuthed,
  throttled, noteFailure, clearFailures,
} from "./auth.js";
import { renderLogin, renderList } from "./page.js";

const STATES = new Set(["new", "interested", "applied", "passed"]);

// no-referrer matters here: every card links out to a job board, and we do not
// want the dashboard's own URL travelling in the Referer header.
const BASE_HEADERS = {
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
};

const html = (body, status = 200, extra = {}) =>
  new Response(body, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", ...BASE_HEADERS, ...extra },
  });

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...BASE_HEADERS },
  });

const redirect = (to, extra = {}) =>
  new Response(null, { status: 302, headers: { Location: to, ...BASE_HEADERS, ...extra } });

async function handleLogin(request, env) {
  if (await throttled(request, env)) {
    return html(renderLogin("Too many attempts. Wait a few minutes."), 429);
  }
  const form = await request.formData();
  const ok = await checkSecret(env, String(form.get("password") || ""), env.DASH_PASSWORD);
  if (!ok) {
    await noteFailure(request, env);
    return html(renderLogin("Wrong password."), 401);
  }
  await clearFailures(request, env);
  return redirect("/", { "Set-Cookie": await mintCookie(env) });
}

async function handleList(request, env) {
  const url = new URL(request.url);
  const tab = STATES.has(url.searchParams.get("tab")) ? url.searchParams.get("tab") : "new";

  const [rows, tally] = await Promise.all([
    env.DB.prepare(
      "SELECT uid, source, title, company, url, posted_at, regions, salary_signal," +
      " score, verdict, why, cv_variant, state FROM jobs WHERE state = ?" +
      " ORDER BY score DESC NULLS LAST, posted_at DESC LIMIT 200",
    ).bind(tab).all(),
    env.DB.prepare("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state").all(),
  ]);

  const counts = { total: 0 };
  for (const r of tally.results) {
    counts[r.state] = r.n;
    counts.total += r.n;
  }
  return html(renderList(rows.results, tab, counts));
}

async function handleState(request, env) {
  const { uid, state } = await request.json().catch(() => ({}));
  if (!uid || !STATES.has(state)) return json({ error: "bad request" }, 400);
  const res = await env.DB.prepare(
    "UPDATE jobs SET state = ?, state_updated_at = ? WHERE uid = ?",
  ).bind(state, new Date().toISOString(), uid).run();
  if (!res.meta.changes) return json({ error: "unknown job" }, 404);
  return json({ ok: true });
}

// Ingest is machine-facing and uses its own bearer token, NOT the session
// cookie -- so the local runner never needs to hold the dashboard password.
async function handleIngest(request, env) {
  const auth = request.headers.get("Authorization") || "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!(await checkSecret(env, token, env.INGEST_TOKEN))) {
    return json({ error: "unauthorized" }, 401);
  }
  const payload = await request.json().catch(() => null);
  if (!Array.isArray(payload)) return json({ error: "expected a JSON array" }, 400);

  const now = new Date().toISOString();
  // ON CONFLICT deliberately omits `state` and `state_updated_at`: a re-push
  // must never reset a job you already triaged.
  const stmt = env.DB.prepare(
    "INSERT INTO jobs (uid, source, title, company, url, posted_at, regions," +
    " salary_signal, score, verdict, why, cv_variant, first_seen)" +
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)" +
    " ON CONFLICT(uid) DO UPDATE SET" +
    "  score = excluded.score, verdict = excluded.verdict, why = excluded.why," +
    "  cv_variant = excluded.cv_variant, salary_signal = excluded.salary_signal," +
    "  regions = excluded.regions, url = excluded.url, title = excluded.title",
  );
  const batch = payload.slice(0, 500).map((j) =>
    stmt.bind(
      j.uid, j.source || "unknown", j.title || "(untitled)", j.company ?? null,
      j.url ?? null, j.posted_at ?? null, j.regions ?? null, j.salary_signal ?? null,
      j.score ?? null, j.verdict ?? null, j.why ?? null, j.cv_variant ?? null, now,
    ),
  );
  if (batch.length) await env.DB.batch(batch);
  return json({ ok: true, received: batch.length });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const { pathname } = url;

    if (pathname === "/ingest" && request.method === "POST") {
      return handleIngest(request, env);
    }
    if (pathname === "/login" && request.method === "POST") {
      return handleLogin(request, env);
    }

    const authed = await isAuthed(request, env);

    if (pathname === "/logout") {
      return redirect("/", { "Set-Cookie": clearCookie() });
    }
    if (!authed) {
      if (pathname.startsWith("/api/")) return json({ error: "unauthorized" }, 401);
      return html(renderLogin(null), 401);
    }
    if (pathname === "/" && request.method === "GET") return handleList(request, env);
    if (pathname === "/api/state" && request.method === "POST") return handleState(request, env);

    return json({ error: "not found" }, 404);
  },
};
