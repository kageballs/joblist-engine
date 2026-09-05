"""Fill in missing advert text for jobs already stored and scored.

Everything stored before 2026-09-03 has an empty `description`: the column
shipped with the cover-letter feature, so every job scored before it has a row
with no advert behind it. `cover.py` refuses those rather than writing a
confident letter about an advert it never read, which is right, but it leaves
high scorers permanently undraftable.

`py main.py --rescore` would fix it as a side effect. This is the cheaper and
safer half of that:

  * **Free.** Refetching a board is HTTP. Only *scoring* costs tokens, and this
    does not score. A whole rehydrate run bills nothing.
  * **Non-destructive.** It writes exactly one column, on rows that have
    nothing in it. Scores, verdicts, requirements and triage state are never
    touched -- so nothing can drop below a threshold it currently clears, which
    a re-score genuinely might: `scorer.py` pins no temperature or seed, so the
    same advert can score differently on a second pass (docs/determinism.md).

    py rehydrate.py                 every stored job with no advert text
    py rehydrate.py --min-score 70  only the ones worth drafting for
    py rehydrate.py --dry-run       say what would be fetched, fetch nothing

An advert that has aged out of its board's feed cannot be recovered; it is
reported as unreachable rather than silently skipped.
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

import config
from sources.himalayas import Himalayas
from sources.onlinejobs import OnlineJobs
from store import Store

# Indeed is deliberately absent. It reads a capture file rather than the
# network (docs/indeed-capture.md), so there is nothing here to refetch: a
# missing Indeed advert needs a new capture taken by hand, not a command.
SOURCES = {"himalayas": Himalayas, "onlinejobs": OnlineJobs}


def log(message: str) -> None:
    print(message, file=sys.stderr)


def targets(store, min_score: int) -> list:
    return store.conn.execute(
        "SELECT uid, source, title, company, score, posted_at FROM jobs"
        " WHERE COALESCE(description, '') = ''"
        "   AND COALESCE(score, 0) >= ?"
        " ORDER BY score DESC",
        (min_score,),
    ).fetchall()


def oldest_posted(rows) -> datetime:
    """How far back the refetch has to reach.

    A board is walked once and matched against every wanted uid at the same
    time, so the window is set by the oldest job being looked for. Falling back
    to 60 days rather than to "forever" keeps a single unparseable date from
    turning into an unbounded crawl.
    """
    stamps = []
    for row in rows:
        try:
            stamps.append(datetime.fromisoformat(row["posted_at"]))
        except (TypeError, ValueError):
            continue
    if not stamps:
        return datetime.now(UTC) - timedelta(days=60)
    return min(stamps) - timedelta(days=1)


def fill(store, rows, dry_run: bool) -> tuple[int, int]:
    """Returns (filled, unreachable)."""
    wanted = {row["uid"]: row for row in rows}
    filled = 0

    for name in sorted({row["source"] for row in rows}):
        mine = {uid: row for uid, row in wanted.items() if row["source"] == name}
        if name not in SOURCES:
            log(f"[skip] {name}: {len(mine)} job(s), but this source is not refetchable")
            continue

        source = SOURCES[name]()
        since = oldest_posted(list(mine.values()))
        log(f"[{name}] looking for {len(mine)} advert(s) back to {since:%Y-%m-%d}")
        if dry_run:
            continue

        found = 0
        try:
            for job in source.fetch(since=since):
                if job.key not in mine:
                    continue
                text = job.description or ""
                # Some boards only carry a teaser in the listing feed. Pay for
                # the extra request only for a job actually being recovered.
                if not text.strip() and hasattr(source, "hydrate"):
                    job = source.hydrate(job)
                    text = job.description or ""
                if not text.strip():
                    continue
                store.conn.execute(
                    "UPDATE jobs SET description = ? WHERE uid = ?", (text, job.key)
                )
                found += 1
                del mine[job.key]
                if not mine:
                    break
        except Exception as exc:  # noqa: BLE001 - one board failing must not lose the other
            log(f"[{name}] fetch failed after {found} recovered: {type(exc).__name__}: {exc}")

        store.conn.commit()
        filled += found
        # "Not found" and "stopped looking" are different answers, and only one
        # of them means the advert is gone. A board walked to its page cap
        # never reached the older end of the window at all, so calling those
        # jobs unreachable would be a conclusion the run did not earn.
        capped = getattr(source, "hit_page_cap", False)
        label = "not reached" if capped else "gone from the board"
        log(f"[{name}] recovered {found}, still missing {len(mine)}"
            + (f" -- STOPPED AT THE {name.upper()} PAGE CAP, so these were never"
               f" looked for; raise it and re-run" if capped else ""))
        for row in sorted(mine.values(), key=lambda r: -(r["score"] or 0)):
            log(f"[{name}]   {label}  {row['score']:>3}  {row['title'][:52]}")

    return filled, len(rows) - filled


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-score", type=int, default=0,
                        help="only jobs scoring at least this much")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be fetched, fetch nothing")
    parser.add_argument("--max-pages", type=int, metavar="N",
                        help="override the per-board page cap for this run only;"
                             " recovering an old advert needs a deeper crawl than"
                             " a daily run does, and this costs requests, not tokens")
    args = parser.parse_args(argv)

    if args.max_pages:
        config.HIMALAYAS_MAX_PAGES = args.max_pages
        config.ONLINEJOBS_MAX_PAGES = args.max_pages
        log(f"[run] page cap raised to {args.max_pages} for this run")

    with Store() as store:
        rows = targets(store, args.min_score)
        if not rows:
            log("[ok] every stored job at or above that score already has advert text.")
            return 0
        log(f"[run] {len(rows)} stored job(s) have no advert text")
        filled, missing = fill(store, rows, args.dry_run)

    if args.dry_run:
        return 0
    log(f"[done] filled {filled}, {missing} still without text")
    if filled:
        log("[done] now run: py cover.py --backfill")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
