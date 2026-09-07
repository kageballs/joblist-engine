"""Push scored jobs to the dashboard.

Only the `jobs` table is ever sent. Rejects stay on this machine: their reasons
name flagged employers, so publishing them would make that list inferable from
the dashboard (see CLAUDE.md, "Privacy split").

    py push.py                # push everything scored
    py push.py --with-covers --url http://127.0.0.1:8787
                              # also send drafted letters (LOCAL dashboard only)
    py push.py --since 7d     # only jobs first seen in the last 7 days
    py push.py --dry-run      # print what would go, send nothing

Two tables go up: `jobs`, and the `job_requirements` behind the Improve tab.
Requirements are the employers' demands, not a record of who was filtered out,
so they carry none of the inference risk that keeps `rejects` local.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import config
import cover
import targeting

FIELDS = (
    "uid", "source", "title", "company", "url", "posted_at",
    "regions", "salary_signal", "score", "verdict", "why", "cv_variant",
    # `score` is already the penalised figure; `score_raw` is what the model
    # said before it, so a card can show the arithmetic rather than a number
    # that looks like a bad review of the work.
    "score_raw", "blockers",
)
CHUNK = 200

# A cover letter is written from the resume and speaks in the candidate's own
# voice about their own history. That makes it more personal than the advert
# text `FIELDS` already keeps off the wire, so it travels only to a dashboard
# running on this machine. Enforced here rather than left to discipline: the
# whole point of a --url flag is that the target changes between invocations.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def is_local(url: str) -> bool:
    return (urllib.parse.urlsplit(url or "").hostname or "").lower() in LOCAL_HOSTS


def collect_covers(uids: list[str]) -> list[dict]:
    """Drafted letters for the jobs being pushed, read off disk.

    Keyed through `cover.slug` rather than a second naming scheme, so a change
    to how letters are named on disk cannot silently stop matching them here.
    """
    rows = []
    for uid in uids:
        path = cover.out_path(uid)
        if not path.exists():
            continue
        body = path.read_text(encoding="utf-8")
        if not body.strip():
            continue
        drafted = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
        rows.append({"uid": uid, "body": body, "drafted_at": drafted})
    return rows


def collect_policy(sources, profile_path=None) -> list[dict]:
    """Each board's age window, for the boards actually being pushed.

    Read through `targeting.load` rather than by parsing profile.yaml here, so
    the dashboard cannot end up applying a window the digest never had. One
    reader, one meaning — a second parser is how two surfaces start disagreeing.

    A board with no block raises in `Profile.board`, deliberately, and that
    raise is caught here rather than allowed to kill a push: an unknown board is
    a reason to leave the dashboard untrimmed for it, not a reason to lose the
    job rows that were about to go up.

    `max_age_days` of None is passed through as null, meaning "never trim", and
    is not the same as omitting the board.
    """
    profile = targeting.load(profile_path or config.PROFILE_PATH)
    rows = []
    for name in sorted(set(sources)):
        try:
            rows.append({"source": name, "max_age_days": profile.board(name).max_age_days})
        except targeting.ProfileError:
            continue
    return rows


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


def collect_requirements(uids: list[str]) -> list[dict]:
    """Requirement rows for the jobs being pushed, and only those.

    Scoped to the same uids rather than the whole table so a `--since` push
    cannot ship rows whose parent job the dashboard has never seen -- those
    would count toward the tally while being un-drillable.
    """
    if not uids:
        return []
    db = Path(config.DB_PATH)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = []
    # SQLite caps host parameters, so chunk the IN list.
    for start in range(0, len(uids), 400):
        window = uids[start:start + 400]
        marks = ",".join("?" * len(window))
        rows += [dict(r) for r in conn.execute(
            f"SELECT uid, kind, mandatory, detail, blocking FROM job_requirements"
            f" WHERE uid IN ({marks})", window)]
    conn.close()
    return rows


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


def post(url: str, token: str, batch: list[dict], path: str = "/ingest") -> dict:
    request = urllib.request.Request(
        url.rstrip("/") + path,
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
    parser.add_argument("--with-covers", action="store_true",
                        help="also send drafted cover letters "
                             "(refused unless --url is localhost)")
    parser.add_argument("--url", default=load_env("DASHBOARD_URL"))
    args = parser.parse_args()

    if args.with_covers and not is_local(args.url):
        sys.exit(
            f"refusing --with-covers against {args.url!r}: cover letters are "
            "written from your resume and go only to a dashboard on this "
            "machine. Run the local one (cd dashboard && npx wrangler dev "
            "--local) and pass --url http://127.0.0.1:8787.")

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
        if args.with_covers:
            print(f"{len(collect_covers([j['uid'] for j in jobs]))} drafted letter(s) "
                  f"would go with them")
        for row in collect_policy({j["source"] for j in jobs}):
            window = row["max_age_days"]
            print(f"  window: {row['source']} -> "
                  + ("never trimmed" if window is None else f"{window} days"))
        return 0

    token = load_env("INGEST_TOKEN")
    if not args.url or not token:
        sys.exit("set DASHBOARD_URL and INGEST_TOKEN in .env (or pass --url)")

    def send(rows: list[dict], path: str, label: str) -> int:
        sent = 0
        for start in range(0, len(rows), CHUNK):
            chunk = rows[start:start + CHUNK]
            try:
                result = post(args.url, token, chunk, path)
            except urllib.error.HTTPError as exc:
                sys.exit(f"{label} push failed ({exc.code}): "
                         f"{exc.read().decode('utf-8', 'replace')[:200]}")
            except urllib.error.URLError as exc:
                sys.exit(f"{label} push failed: {exc.reason}")
            sent += result.get("received", 0)
        return sent

    uids = [j["uid"] for j in jobs]
    sent = send(jobs, "/ingest", "jobs")
    reqs = collect_requirements(uids)
    sent_reqs = send(reqs, "/ingest/requirements", "requirements") if reqs else 0
    # Sent every push, not once at setup: the window is retuned by editing
    # profile.yaml, and a dashboard still applying last month's number is
    # exactly the disagreement this is here to close.
    policy = collect_policy({j["source"] for j in jobs})
    sent_policy = send(policy, "/ingest/policy", "policy") if policy else 0
    line = (f"pushed {sent} job(s), {sent_reqs} requirement row(s) and "
            f"{sent_policy} board window(s) to {args.url}")

    if args.with_covers:
        letters = collect_covers(uids)
        # Letters are large next to a job row, so they go up in smaller batches
        # than CHUNK would allow.
        sent_covers = 0
        for start in range(0, len(letters), 20):
            sent_covers += send(letters[start:start + 20], "/ingest/covers", "covers")
        line += f", plus {sent_covers} cover letter(s)"

    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
