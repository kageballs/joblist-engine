"""What employers keep asking you to produce.

    py report.py                what is asked for, most frequent first
    py report.py --source onlinejobs   one board only
    py report.py --since 30d    only jobs first seen in that window
    py report.py --detail work_samples   the actual sentences behind one kind
    py report.py --blocked      only listings you currently cannot apply to
    py report.py --reapply      re-price stored scores against the current list

Two questions, one table. **What is asked** tells you what to go build or
obtain, ranked by how often it costs you something. **What blocks you** is the
subset you have flagged in `profile.yaml` under `deliverables.cannot_provide`.

`blocking` is recomputed from the live profile on every run of this report, not
read back from what was stored, so adding a key to `cannot_provide` re-flags
your whole history immediately. `--reapply` then writes the matching penalty
back into the stored scores, so the digest and dashboard agree with the report.
Neither costs an API call: that is the point of applying the penalty locally
rather than asking the model to do it.

Reads the local store only. Nothing here is pushed anywhere: a requirement tally
describes what you cannot do, which is exactly the kind of thing the privacy
split in CLAUDE.md exists to keep on this machine.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import config
import targeting


def parse_since(text: str) -> timedelta:
    unit = text[-1].lower()
    value = int(text[:-1])
    if unit == "h":
        return timedelta(hours=value)
    if unit == "d":
        return timedelta(days=value)
    if unit == "w":
        return timedelta(weeks=value)
    raise argparse.ArgumentTypeError("use forms like 24h, 30d, 2w")


def connect() -> sqlite3.Connection:
    db = Path(config.DB_PATH)
    if not db.exists():
        sys.exit(f"no store at {db} — run main.py first")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _where(args) -> tuple[str, list]:
    clauses, params = [], []
    if args.source:
        clauses.append("j.source = ?")
        params.append(args.source)
    if args.since is not None:
        clauses.append("j.created_at >= ?")
        params.append((datetime.now(UTC) - args.since).isoformat())
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def _bar(n: int, top: int, width: int = 22) -> str:
    if top <= 0:
        return ""
    filled = max(1, round(n / top * width)) if n else 0
    return "#" * filled + "." * (width - filled)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--source", default=None)
    parser.add_argument("--since", type=parse_since, default=None)
    parser.add_argument("--detail", default=None, metavar="KIND")
    parser.add_argument("--blocked", action="store_true")
    parser.add_argument("--reapply", action="store_true",
                        help="recompute stored scores against the current cannot_provide list")
    parser.add_argument("--profile", default=config.PROFILE_PATH)
    parser.add_argument("-h", "--help", action="store_true")
    args = parser.parse_args()

    if args.help:
        print(__doc__)
        return 0

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")

    try:
        profile = targeting.load(args.profile)
    except targeting.ProfileError as exc:
        print("[error] " + str(exc), file=sys.stderr)
        return 1

    conn = connect()
    where, params = _where(args)

    scored = conn.execute(
        f"SELECT COUNT(*) AS n FROM jobs j{where}", params
    ).fetchone()["n"]
    if not scored:
        print("no scored jobs match that filter")
        return 0

    # --reapply: re-derive every stored score from its raw score and the
    # CURRENT list. No API calls -- this is the whole reason the penalty is
    # applied locally instead of by the model. Editing cannot_provide, or
    # changing blocker_penalty, is meant to re-price your entire history for
    # free; without this the report would flag listings whose stored score, and
    # therefore the dashboard, still reflected the old list.
    if args.reapply:
        # Rows scored before this feature have no score_raw. They were never
        # penalised, so their score IS their raw score.
        conn.execute("UPDATE jobs SET score_raw = score WHERE score_raw IS NULL")

        rows = conn.execute(
            "SELECT j.uid, j.score_raw, j.score,"
            " (SELECT GROUP_CONCAT(r.kind) FROM job_requirements r"
            "   WHERE r.uid = j.uid AND r.mandatory = 1) AS musts"
            " FROM jobs j"
        ).fetchall()

        changed = 0
        for row in rows:
            musts = set((row["musts"] or "").split(",")) - {""}
            blocking = sorted(musts & set(profile.cannot_provide))
            raw = row["score_raw"]
            new = raw if raw is None or not blocking else max(
                0, int(raw) - profile.blocker_penalty * len(blocking)
            )
            if new != row["score"] or blocking:
                conn.execute(
                    "UPDATE jobs SET score = ?, blockers = ? WHERE uid = ?",
                    (new, json.dumps(blocking), row["uid"]),
                )
                conn.execute("UPDATE job_requirements SET blocking = 0 WHERE uid = ?", (row["uid"],))
                for kind in blocking:
                    conn.execute(
                        "UPDATE job_requirements SET blocking = 1 WHERE uid = ? AND kind = ?",
                        (row["uid"], kind),
                    )
            if new != row["score"]:
                changed += 1
        conn.commit()
        print(f"re-priced {len(rows)} stored job(s) against "
              f"cannot_provide={sorted(profile.cannot_provide)} "
              f"at -{profile.blocker_penalty}/blocker")
        print(f"{changed} score(s) changed. Push to the dashboard with: py push.py")
        conn.close()
        return 0

    # --detail: the actual sentences behind one requirement kind.
    if args.detail:
        if args.detail not in config.REQUIREMENT_KINDS:
            print(f"unknown kind {args.detail!r}. Valid: "
                  + ", ".join(sorted(config.REQUIREMENT_KINDS)), file=sys.stderr)
            return 1
        rows = conn.execute(
            f"SELECT j.title, j.company, j.source, r.mandatory, r.detail"
            f" FROM job_requirements r JOIN jobs j ON j.uid = r.uid{where}"
            + (" AND" if where else " WHERE") + " r.kind = ?"
            " ORDER BY r.mandatory DESC, j.created_at DESC",
            params + [args.detail],
        ).fetchall()
        print(f"\n{args.detail} — {config.REQUIREMENT_KINDS[args.detail]}")
        print(f"{len(rows)} listing(s)\n")
        for r in rows:
            mark = "MUST" if r["mandatory"] else "nice"
            print(f"  [{mark}] {r['title'][:56]}")
            if r["company"]:
                print(f"         {r['company']} · {r['source']}")
            if r["detail"]:
                print(f"         \"{r['detail']}\"")
        conn.close()
        return 0

    # --blocked: the listings you currently cannot apply to.
    if args.blocked:
        if not profile.cannot_provide:
            print("deliverables.cannot_provide is empty — nothing is flagged yet.")
            conn.close()
            return 0
        marks = ",".join("?" * len(profile.cannot_provide))
        rows = conn.execute(
            f"SELECT j.title, j.company, j.source, j.score, j.score_raw,"
            f" GROUP_CONCAT(r.kind) AS kinds"
            f" FROM jobs j JOIN job_requirements r ON r.uid = j.uid{where}"
            + (" AND" if where else " WHERE")
            + f" r.mandatory = 1 AND r.kind IN ({marks})"
            " GROUP BY j.uid ORDER BY j.score_raw DESC",
            params + sorted(profile.cannot_provide),
        ).fetchall()
        print(f"\n{len(rows)} listing(s) you cannot currently apply to\n")
        for r in rows:
            raw, adj = r["score_raw"], r["score"]
            move = f"{raw} -> {adj}" if raw is not None and raw != adj else str(adj)
            print(f"  {move:>10}  {r['title'][:52]}")
            print(f"              {r['company'] or 'unknown'} · {r['source']} · needs: {r['kinds']}")
        conn.close()
        return 0

    # Default: the tally.
    rows = conn.execute(
        f"SELECT r.kind,"
        f" COUNT(*) AS listings,"
        f" SUM(r.mandatory) AS must"
        f" FROM job_requirements r JOIN jobs j ON j.uid = r.uid{where}"
        " GROUP BY r.kind ORDER BY listings DESC, must DESC",
        params,
    ).fetchall()

    if not rows:
        print(f"{scored} scored job(s), but no requirements recorded yet.\n"
              "Requirements are extracted during scoring, so only runs made after\n"
              "this feature landed have them. Re-run with: py main.py --rescore")
        conn.close()
        return 0

    covered = conn.execute(
        f"SELECT COUNT(DISTINCT r.uid) AS n FROM job_requirements r"
        f" JOIN jobs j ON j.uid = r.uid{where}", params
    ).fetchone()["n"]

    top = max(r["listings"] for r in rows)
    scope = []
    if args.source:
        scope.append(args.source)
    if args.since is not None:
        scope.append(f"last {args.since.days or 1}d")
    header = "WHAT EMPLOYERS ASK YOU TO PRODUCE"
    if scope:
        header += "  (" + ", ".join(scope) + ")"

    print("\n" + header)
    print(f"{covered} of {scored} scored listings asked for something.\n")
    print(f"  {'':<23}{'ads':>5} {'must':>5}  {'':<22}")
    for r in rows:
        kind, n, must = r["kind"], r["listings"], r["must"] or 0
        flag = " <- YOU CANNOT PROVIDE" if kind in profile.cannot_provide else ""
        pct = n / scored * 100
        print(f"  {kind:<23}{n:>5} {must:>5}  {_bar(n, top)}  {pct:4.0f}%{flag}")

    print("\n  ads  = listings that asked for it at all")
    print("  must = listings that made it a condition, not a preference")

    if profile.cannot_provide:
        blocked = conn.execute(
            f"SELECT COUNT(DISTINCT r.uid) AS n FROM job_requirements r"
            f" JOIN jobs j ON j.uid = r.uid{where}"
            + (" AND" if where else " WHERE")
            + f" r.mandatory = 1 AND r.kind IN ({','.join('?' * len(profile.cannot_provide))})",
            params + sorted(profile.cannot_provide),
        ).fetchone()["n"]
        print(f"\n  {blocked} listing(s) ({blocked / scored * 100:.0f}%) are blocked for you today"
              f" — each loses {profile.blocker_penalty} points per unmet condition.")
        print("  py report.py --blocked          which ones")
    else:
        print("\n  Nothing flagged yet. Add the keys you cannot supply to profile.yaml:")
        print("    deliverables:\n      cannot_provide: [portfolio_screenshots]")
    print("  py report.py --detail <kind>    the actual sentences\n")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
