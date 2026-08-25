"""Push scored jobs to the dashboard.

Only the `jobs` table is ever sent. Rejects stay on this machine: their reasons
name flagged employers, so publishing them would make that list inferable from
the dashboard (see CLAUDE.md, "Privacy split").

    py push.py                # push everything scored
    py push.py --since 7d     # only jobs first seen in the last 7 days
    py push.py --dry-run      # print what would go, send nothing
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import config

FIELDS = (
    "uid", "source", "title", "company", "url", "posted_at",
    "regions", "salary_signal", "score", "verdict", "why", "cv_variant",
)
CHUNK = 200


def load_env(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    env_file = Path(".env")
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def parse_since(text: str) -> timedelta:
    unit = text[-1].lower()
    value = int(text[:-1])
    if unit == "h":
        return timedelta(hours=value)
    if unit == "d":
        return timedelta(days=value)
    raise ValueError(f"unrecognised --since {text!r}; use e.g. 24h or 7d")


def collect(since: timedelta | None) -> list[dict]:
    db = Path(config.DB_PATH) if hasattr(config, "DB_PATH") else Path("data/joblist.sqlite3")
    if not db.exists():
        sys.exit(f"no store at {db} — run main.py first")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    sql = f"SELECT {', '.join(FIELDS)} FROM jobs"
    params: tuple = ()
    if since is not None:
        sql += " WHERE created_at >= ?"
        params = ((datetime.now(UTC) - since).isoformat(),)
    rows = [dict(r) for r in conn.execute(sql, params)]
    conn.close()
    return rows


def post(url: str, token: str, batch: list[dict]) -> dict:
    request = urllib.request.Request(
        url.rstrip("/") + "/ingest",
        data=json.dumps(batch).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            # Cloudflare's bot check answers Python-urllib's default agent with
            # a 403 (error 1010) before the Worker is ever reached.
            "User-Agent": "joblist-push/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=parse_since, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--url", default=load_env("DASHBOARD_URL"))
    args = parser.parse_args()

    jobs = collect(args.since)
    if not jobs:
        print("nothing to push")
        return 0

    if args.dry_run:
        print(f"{len(jobs)} job(s) would be pushed to {args.url or '<no DASHBOARD_URL>'}:")
        for job in sorted(jobs, key=lambda j: -(j["score"] or 0))[:10]:
            print(f"  {job['score'] or '--':>3}  {job['company']}  {job['title'][:52]}")
        if len(jobs) > 10:
            print(f"  ... and {len(jobs) - 10} more")
        return 0

    token = load_env("INGEST_TOKEN")
    if not args.url or not token:
        sys.exit("set DASHBOARD_URL and INGEST_TOKEN in .env (or pass --url)")

    sent = 0
    for start in range(0, len(jobs), CHUNK):
        chunk = jobs[start:start + CHUNK]
        try:
            result = post(args.url, token, chunk)
        except urllib.error.HTTPError as exc:
            sys.exit(f"push failed ({exc.code}): {exc.read().decode('utf-8', 'replace')[:200]}")
        except urllib.error.URLError as exc:
            sys.exit(f"push failed: {exc.reason}")
        sent += result.get("received", 0)
    print(f"pushed {sent} job(s) to {args.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
