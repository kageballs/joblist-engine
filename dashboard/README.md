# joblist dashboard

Password-gated triage queue for scored jobs, on Cloudflare Workers + D1.

Local `main.py` stays the engine. This is a read/act surface over its output:
scored jobs sorted high-to-low, each with its apply link and source platform,
and a state you set — interested / applied / passed — that lives in D1 so the
phone and the desktop agree.

## Privacy contract

**Only the `jobs` table is ever pushed.** Rejects stay on the local machine:
their reasons name flagged employers, so publishing them would make that list
inferable from the dashboard. `schema.sql` has no rejects table and `push.py`
never selects from one. Keep it that way.

## Layout

```
dashboard/
  wrangler.toml     config + non-secret vars
  schema.sql        jobs + login_attempts
  src/index.js      router: /, /login, /logout, /api/state, /ingest
  src/auth.js       password check, HMAC cookie, login throttling
  src/page.js       server-rendered HTML + CSS (no framework, no build step)
../push.py          reads local SQLite, POSTs to /ingest
```

## Auth

One password, checked against the `DASH_PASSWORD` secret, sets an HMAC-signed
cookie carrying its own expiry (`SESSION_DAYS`, default 60). There is no
sessions table — **rotating `COOKIE_SECRET` invalidates every live session at
once**, which is the whole revocation story.

Failed logins are counted per IP in D1: `MAX_LOGIN_FAILS` within
`LOGIN_WINDOW_MIN` minutes returns 429 until the window lapses.

`/ingest` does **not** use the cookie. It takes its own `INGEST_TOKEN` bearer,
so the local runner never holds the dashboard password.

Every response sends `Referrer-Policy: no-referrer` — every card links out to a
job board, and the dashboard URL should not ride along in the `Referer` header.

## First deploy

Needs a valid Cloudflare token at `C:\Users\ADMIN\.cf-token`.

```bash
export CLOUDFLARE_API_TOKEN=$(cat /c/Users/ADMIN/.cf-token)

npx wrangler d1 create joblist          # paste the id into wrangler.toml
npm run init-remote                     # apply schema.sql

npx wrangler secret put DASH_PASSWORD   # what you type at the login screen
npx wrangler secret put COOKIE_SECRET   # e.g. openssl rand -base64 32
npx wrangler secret put INGEST_TOKEN    # e.g. openssl rand -base64 32

npm run deploy                          # -> https://<worker>.<subdomain>.workers.dev
```

Then in `../.env` (gitignored):

```
DASHBOARD_URL=https://<worker>.<subdomain>.workers.dev
INGEST_TOKEN=<the same value>
```

D1 free tier allows 10 databases; check `npx wrangler d1 list` before creating.

## Two things that will bite on a fresh deploy

**Set secrets without a trailing newline.** `wrangler secret put` stores stdin
verbatim, and PowerShell's pipe appends CRLF. The Worker compares the bearer
and the password exactly, so a secret set with `$value | wrangler secret put`
yields a 401 that looks like a wrong token. Use `printf '%s' "$value" |
npx wrangler secret put NAME`.

**`push.py` must send a User-Agent.** Cloudflare's bot check answers
`Python-urllib/3.x` with a 403 (error code 1010) before the request reaches the
Worker, so the failure looks like the Worker rejecting you when it never saw it.

## Daily use

```bash
py main.py        # fetch, filter, score, write the digest
py push.py        # mirror scored jobs to the dashboard
```

`push.py --dry-run` prints what would go without sending. Re-pushing is safe:
the upsert deliberately omits `state`, so a job you already marked applied is
never reset by a later run.

## Local development

```bash
cd dashboard
npm run init-local        # schema into the miniflare D1
npm run dev               # http://127.0.0.1:8788
```

Local secrets live in `.dev.vars` (gitignored). No Cloudflare account needed —
`wrangler dev --local` runs D1 in-process.

Push local data at it with:

```bash
INGEST_TOKEN=<from .dev.vars> py push.py --url http://127.0.0.1:8788
```
