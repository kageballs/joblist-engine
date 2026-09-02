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


def _where(args, source=None) -> tuple[str, list]:
    """`source` overrides `args.source` -- used to scope one board's own
    section without re-parsing args. See `_boards_in`: a count or percentage
    pooled across boards would blend, say, OnlineJobs asking for something in
    37% of its listings into Himalayas' 5%, hiding exactly the split that
    makes the number worth reading. Both figures are from this repo's own
    store, 2026-09-02: 32 of 86 against 8 of 169."""
    clauses, params = [], []
    board = args.source if source is None else source
    if board:
        clauses.append("j.source = ?")
        params.append(board)
    if args.since is not None:
        clauses.append("j.created_at >= ?")
        params.append((datetime.now(UTC) - args.since).isoformat())
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def _boards_in(conn, where: str, params: list) -> list[str]:
    """Distinct boards present in the current filter, sorted.

    Every listing/tally view sections by this instead of pooling: a score, a
    rate, or a percentage is only meaningful within the board that produced
    it (see targeting.BoardPolicy).
    """
    return [r["source"] for r in conn.execute(
        f"SELECT DISTINCT j.source FROM jobs j{where} ORDER BY j.source", params
    )]


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

        # manual_steps is the third, deliberately unpriced category: a job
        # that asks for a video intro or a test task is fully winnable, just
        # not in one sitting, so it never touches score or score_raw -- it is
        # re-derived here, live, from ALL requirement kinds against
        # needs_manual_step, not just the mandatory ones (a "nice to have"
        # test task still costs the same evening as a required one; see
        # blockers.evaluate). Sectioned by board like every other listing
        # view here -- --source/--since narrow which rows print, they do not
        # touch the re-pricing above, which stays global by design.
        if profile.needs_manual_step:
            marks = ",".join("?" * len(profile.needs_manual_step))
            manual_boards = _boards_in(conn, where, params)
            manual_total = 0
            for board_name in manual_boards:
                board_where, board_params = _where(args, source=board_name)
                manual_rows = conn.execute(
                    "SELECT j.uid, j.title, j.company,"
                    " GROUP_CONCAT(r.kind) AS kinds"
                    f" FROM jobs j JOIN job_requirements r ON r.uid = j.uid{board_where}"
                    f" AND r.kind IN ({marks})"
                    " GROUP BY j.uid ORDER BY j.created_at DESC",
                    board_params + sorted(profile.needs_manual_step),
                ).fetchall()
                if not manual_rows:
                    continue
                manual_total += len(manual_rows)
                print(f"\n## {board_name} — {len(manual_rows)} stored job(s) need a manual "
                      f"step before applying (needs_manual_step="
                      f"{sorted(profile.needs_manual_step)}, never priced):")
                for r in manual_rows:
                    print(f"  {r['title'][:52]}  ({r['company'] or 'unknown'}) "
                          f"needs: {r['kinds']}")
            if not manual_total:
                print(f"\n0 stored job(s) need a manual step before applying "
                      f"(needs_manual_step={sorted(profile.needs_manual_step)}, never priced)")

        conn.close()
        return 0

    # --detail: the actual sentences behind one requirement kind.
    if args.detail:
        if args.detail not in config.REQUIREMENT_KINDS:
            print(f"unknown kind {args.detail!r}. Valid: "
                  + ", ".join(sorted(config.REQUIREMENT_KINDS)), file=sys.stderr)
            return 1
        print(f"\n{args.detail} — {config.REQUIREMENT_KINDS[args.detail]}")
        # Sectioned by board, same as every other listing view (_boards_in):
        # a company is only ever known for himalayas rows, so printing the
        # board only when company is set silently dropped it for every
        # onlinejobs listing.
        boards = _boards_in(conn, where, params)
        total = 0
        for board_name in boards:
            board_where, board_params = _where(args, source=board_name)
            rows = conn.execute(
                f"SELECT j.title, j.company, r.mandatory, r.detail"
                f" FROM job_requirements r JOIN jobs j ON j.uid = r.uid{board_where}"
                " AND r.kind = ?"
                " ORDER BY r.mandatory DESC, j.created_at DESC",
                board_params + [args.detail],
            ).fetchall()
            if not rows:
                continue
            total += len(rows)
            print(f"\n## {board_name} — {len(rows)} listing(s)")
            for r in rows:
                mark = "MUST" if r["mandatory"] else "nice"
                print(f"  [{mark}] {r['title'][:56]}")
                print(f"         {r['company'] or 'unknown'} · {board_name}")
                if r["detail"]:
                    print(f"         \"{r['detail']}\"")
        if not total:
            print("0 listing(s)")
        conn.close()
        return 0

    # --blocked: the listings you currently cannot apply to.
    if args.blocked:
        if not profile.cannot_provide:
            print("deliverables.cannot_provide is empty — nothing is flagged yet.")
            conn.close()
            return 0
        marks = ",".join("?" * len(profile.cannot_provide))
        boards = _boards_in(conn, where, params)
        total = 0
        # Sectioned by board and ranked within it, never pooled: score_raw is
        # only comparable to another score_raw from the SAME board (see
        # digest._partition), so one list ordered across boards would rank a
        # Himalayas 40 above an OnlineJobs 20 for no reason the numbers mean.
        for board_name in boards:
            board_where, board_params = _where(args, source=board_name)
            rows = conn.execute(
                f"SELECT j.title, j.company, j.score, j.score_raw,"
                f" GROUP_CONCAT(r.kind) AS kinds"
                f" FROM jobs j JOIN job_requirements r ON r.uid = j.uid{board_where}"
                f" AND r.mandatory = 1 AND r.kind IN ({marks})"
                " GROUP BY j.uid ORDER BY j.score_raw DESC",
                board_params + sorted(profile.cannot_provide),
            ).fetchall()
            if not rows:
                continue
            total += len(rows)
            print(f"\n## {board_name} — {len(rows)} listing(s) you cannot currently apply to")
            for r in rows:
                raw, adj = r["score_raw"], r["score"]
                move = f"{raw} -> {adj}" if raw is not None and raw != adj else str(adj)
                print(f"  {move:>10}  {r['title'][:52]}")
                print(f"              {r['company'] or 'unknown'} · needs: {r['kinds']}")
        if not total:
            print("\n0 listing(s) you cannot currently apply to")
        conn.close()
        return 0

    # Default: the tally, sectioned by board. A percentage pooled across
    # boards blends, say, OnlineJobs asking for something in 37% of its
    # listings into Himalayas' 5% -- 32 of 86 against 8 of 169 on this repo's
    # own store, and exactly what a single blended figure would hide.
    boards = _boards_in(conn, where, params)

    # Collected before the header prints: the "no requirements recorded"
    # message below is itself a header-less body, so printing the header up
    # front produced one with nothing under it whenever every board came back
    # empty.
    board_data = []
    for board_name in boards:
        board_where, board_params = _where(args, source=board_name)
        rows = conn.execute(
            f"SELECT r.kind,"
            f" COUNT(*) AS listings,"
            f" SUM(r.mandatory) AS must"
            f" FROM job_requirements r JOIN jobs j ON j.uid = r.uid{board_where}"
            " GROUP BY r.kind ORDER BY listings DESC, must DESC",
            board_params,
        ).fetchall()
        if not rows:
            continue
        board_scored = conn.execute(
            f"SELECT COUNT(*) AS n FROM jobs j{board_where}", board_params
        ).fetchone()["n"]
        covered = conn.execute(
            f"SELECT COUNT(DISTINCT r.uid) AS n FROM job_requirements r"
            f" JOIN jobs j ON j.uid = r.uid{board_where}", board_params
        ).fetchone()["n"]
        board_data.append((board_name, board_where, board_params, rows, board_scored, covered))

    if not board_data:
        print(f"{scored} scored job(s), but no requirements recorded yet.\n"
              "Requirements are extracted during scoring, so only runs made after\n"
              "this feature landed have them. Re-run with: py main.py --rescore")
        conn.close()
        return 0

    scope = ([args.source] if args.source else []) \
        + ([f"last {args.since.days or 1}d"] if args.since is not None else [])
    header = "WHAT EMPLOYERS ASK YOU TO PRODUCE"
    if scope:
        header += "  (" + ", ".join(scope) + ")"
    print("\n" + header)

    for board_name, board_where, board_params, rows, board_scored, covered in board_data:
        top = max(r["listings"] for r in rows)

        print(f"\n## {board_name} — {covered} of {board_scored} scored listings asked for something")
        print(f"  {'':<23}{'ads':>5} {'must':>5}  {'':<22}")
        for r in rows:
            kind, n, must = r["kind"], r["listings"], r["must"] or 0
            flag = " <- YOU CANNOT PROVIDE" if kind in profile.cannot_provide else ""
            pct = n / board_scored * 100
            print(f"  {kind:<23}{n:>5} {must:>5}  {_bar(n, top)}  {pct:4.0f}%{flag}")

        if profile.cannot_provide:
            board_blocked = conn.execute(
                f"SELECT COUNT(DISTINCT r.uid) AS n FROM job_requirements r"
                f" JOIN jobs j ON j.uid = r.uid{board_where}"
                f" AND r.mandatory = 1 AND r.kind IN ({','.join('?' * len(profile.cannot_provide))})",
                board_params + sorted(profile.cannot_provide),
            ).fetchone()["n"]
            print(f"  {board_blocked} listing(s) ({board_blocked / board_scored * 100:.0f}%) "
                  f"blocked for you today on {board_name} — each loses {profile.blocker_penalty} "
                  f"points per unmet condition.")

    print("\n  ads  = listings that asked for it at all")
    print("  must = listings that made it a condition, not a preference")

    if profile.cannot_provide:
        print("  py report.py --blocked          which ones")
    else:
        print("\n  Nothing flagged yet. Add the keys you cannot supply to profile.yaml:")
        print("    deliverables:\n      cannot_provide: [portfolio_screenshots]")
    print("  py report.py --detail <kind>    the actual sentences\n")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
