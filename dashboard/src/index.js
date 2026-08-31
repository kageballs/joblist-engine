import {
  checkSecret, mintCookie, clearCookie, isAuthed,
  throttled, noteFailure, clearFailures,
} from "./auth.js";
import { renderLogin, renderList, renderImprove } from "./page.js";

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

  // Which boards exist is data, not a constant: a new source appears here the
  // first time it ingests a row, with no deploy.
  const boards = await env.DB.prepare(
    "SELECT source, COUNT(*) AS n FROM jobs GROUP BY source ORDER BY source",
  ).all();
  // `src` is user input. It is bound, never interpolated, and additionally
  // checked against the sources actually present so an unknown value shows
  // everything rather than an empty page.
  const asked = url.searchParams.get("src");
  const source = boards.results.some((b) => b.source === asked) ? asked : null;

  const filter = source ? "state = ? AND source = ?" : "state = ?";
  const binds = source ? [tab, source] : [tab];

  const [rows, tally] = await Promise.all([
    env.DB.prepare(
      "SELECT uid, source, title, company, url, posted_at, regions, salary_signal," +
      ` score, verdict, why, cv_variant, state, score_raw, blockers FROM jobs WHERE ${filter}` +
      " ORDER BY score DESC NULLS LAST, posted_at DESC LIMIT 200",
    ).bind(...binds).all(),
    // The per-state tally has to respect the source filter too, or the tab
    // badges advertise counts the filtered list will not show.
    source
      ? env.DB.prepare("SELECT state, COUNT(*) AS n FROM jobs WHERE source = ? GROUP BY state")
          .bind(source).all()
      : env.DB.prepare("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state").all(),
  ]);

  const counts = { total: 0 };
  for (const r of tally.results) {
    counts[r.state] = r.n;
    counts.total += r.n;
  }
  // The "All" tab's own badge must stay unscoped, or selecting a board makes
  // the tab that clears the filter advertise the filtered count.
  counts.allTotal = boards.results.reduce((sum, b) => sum + b.n, 0);
  return html(renderList(rows.results, tab, counts, boards.results, source));
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
    " salary_signal, score, verdict, why, cv_variant, first_seen, score_raw, blockers)" +
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)" +
    " ON CONFLICT(uid) DO UPDATE SET" +
    "  score = excluded.score, verdict = excluded.verdict, why = excluded.why," +
    "  cv_variant = excluded.cv_variant, salary_signal = excluded.salary_signal," +
    "  regions = excluded.regions, url = excluded.url, title = excluded.title," +
    "  score_raw = excluded.score_raw, blockers = excluded.blockers",
  );
  const batch = payload.slice(0, 500).map((j) =>
    stmt.bind(
      j.uid, j.source || "unknown", j.title || "(untitled)", j.company ?? null,
      j.url ?? null, j.posted_at ?? null, j.regions ?? null, j.salary_signal ?? null,
      j.score ?? null, j.verdict ?? null, j.why ?? null, j.cv_variant ?? null, now,
      j.score_raw ?? null, j.blockers ?? null,
    ),
  );
  if (batch.length) await env.DB.batch(batch);
  return json({ ok: true, received: batch.length });
}

async function handleIngestRequirements(request, env) {
  const auth = request.headers.get("Authorization") || "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!(await checkSecret(env, token, env.INGEST_TOKEN))) {
    return json({ error: "unauthorized" }, 401);
  }
  const payload = await request.json().catch(() => null);
  if (!Array.isArray(payload)) return json({ error: "expected a JSON array" }, 400);

  const rows = payload.slice(0, 1000).filter((r) => r && r.uid && r.kind);
  // Replace per job rather than accumulating: a re-scored listing whose ad was
  // edited must not keep the requirements of its previous pass alongside the
  // new ones, or the tally counts asks that are no longer being made.
  const uids = [...new Set(rows.map((r) => r.uid))];
  const wipe = env.DB.prepare("DELETE FROM job_requirements WHERE uid = ?");
  const ins = env.DB.prepare(
    "INSERT OR REPLACE INTO job_requirements (uid, kind, mandatory, detail, blocking)" +
    " VALUES (?,?,?,?,?)",
  );
  const batch = [
    ...uids.map((uid) => wipe.bind(uid)),
    ...rows.map((r) => ins.bind(
      r.uid, String(r.kind), r.mandatory ? 1 : 0, r.detail ?? null, r.blocking ? 1 : 0,
    )),
  ];
  if (batch.length) await env.DB.batch(batch);
  return json({ ok: true, received: rows.length });
}

// The Improve tab: one row per requirement kind, never per listing.
//
// It answers "what do I keep getting asked for that I cannot give them", so it
// is an aggregate by construction. (uid, kind) is the table's primary key, so a
// single listing can never contribute twice to the same kind and COUNT(*) is
// already a distinct-listing count.
async function handleImprove(request, env) {
  const url = new URL(request.url);

  const boards = await env.DB.prepare(
    "SELECT source, COUNT(*) AS n FROM jobs GROUP BY source ORDER BY source",
  ).all();
  const asked = url.searchParams.get("src");
  const source = boards.results.some((b) => b.source === asked) ? asked : null;

  const filter = source ? " WHERE j.source = ?" : "";
  const binds = source ? [source] : [];

  const [kinds, scored, counted] = await Promise.all([
    env.DB.prepare(
      "SELECT r.kind, COUNT(*) AS listings, SUM(r.mandatory) AS must," +
      " MAX(r.blocking) AS blocking" +
      " FROM job_requirements r JOIN jobs j ON j.uid = r.uid" + filter +
      " GROUP BY r.kind ORDER BY listings DESC, must DESC",
    ).bind(...binds).all(),
    env.DB.prepare("SELECT COUNT(*) AS n FROM jobs j" + filter).bind(...binds).all(),
    env.DB.prepare(
      "SELECT COUNT(DISTINCT r.uid) AS n FROM job_requirements r" +
      " JOIN jobs j ON j.uid = r.uid" + filter,
    ).bind(...binds).all(),
  ]);

  // Up to three real sentences per kind, so a row says what to actually go and
  // build rather than just naming a category.
  const samples = await env.DB.prepare(
    "SELECT r.kind, r.detail FROM job_requirements r JOIN jobs j ON j.uid = r.uid" +
    (filter ? filter + " AND" : " WHERE") + " r.detail IS NOT NULL AND r.detail != ''" +
    " ORDER BY r.mandatory DESC",
  ).bind(...binds).all();
  // Dedupe on the text, not just per listing. Employers repost the same ad
  // verbatim (OLJ is full of it), so three "examples" can otherwise be one
  // sentence printed three times, which reads as a rendering bug.
  const byKind = {};
  const seen = {};
  for (const row of samples.results) {
    const key = String(row.detail).trim().toLowerCase();
    const bucket = (byKind[row.kind] ||= []);
    const taken = (seen[row.kind] ||= new Set());
    if (bucket.length < 3 && !taken.has(key)) {
      taken.add(key);
      bucket.push(row.detail);
    }
  }

  return html(renderImprove(
    kinds.results, byKind, boards.results, source,
    scored.results[0]?.n || 0, counted.results[0]?.n || 0,
  ));
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const { pathname } = url;

    if (pathname === "/ingest" && request.method === "POST") {
      return handleIngest(request, env);
    }
    if (pathname === "/ingest/requirements" && request.method === "POST") {
      return handleIngestRequirements(request, env);
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
    if (pathname === "/improve" && request.method === "GET") return handleImprove(request, env);
    if (pathname === "/" && request.method === "GET") return handleList(request, env);
    if (pathname === "/api/state" && request.method === "POST") return handleState(request, env);

    return json({ error: "not found" }, 404);
  },
};
