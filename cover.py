"""Draft a cover letter for a job already stored and scored.

Deliberately NOT part of `main.py`. A run scores every survivor because
ranking is what makes the digest worth opening, but a letter is only worth
writing for a posting a human has decided to apply to. Drafting one per
scored job would spend a long call on the ones that never get sent, which is
the same mistake `filters.py` exists to avoid at the other end of the funnel.

So the trigger is explicit:

    py cover.py <uid>            draft for one job (a unique prefix works)
    py cover.py <uid> <uid> ...  several
    py cover.py --top 5          the 5 highest-scoring stored jobs
    py cover.py <uid> --dry-run  print the prompt, call nothing

Output is a markdown file per job under `data/covers/`, never sent anywhere.
The letter is a draft for a human to edit: that is the whole point of the
feature, and the file says so at the top so a rushed copy-paste cannot mistake
it for finished text.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import pathlib
import re
import sys

import config
import targeting

# The candidate's own writing rules. These are not stylistic preferences: a
# letter that breaks them contradicts the CV it is sent with, which is worse
# than a bland letter. Kept here as prose rather than as a post-hoc validator
# because the model is being asked to write, not to be corrected afterwards.
VOICE = """HOUSE STYLE, all mandatory
- No em dashes anywhere. Use commas, colons, periods or parentheses.
- Never name a past client or employer the resume does not name.
- Never state a total years-of-experience figure. Date ranges are fine.
- Plain declarative sentences. No "I am passionate about", no "I believe I
  would be a great fit", no restating the job title back at them.
- Lead with a result, not with wanting the job."""

INSTRUCTIONS = """You are drafting a cover letter for the candidate below, for
the specific advert below. A human will edit it before it is sent.

RULES
1. Every claim must be traceable to the CANDIDATE RESUME. If the advert asks
   for something the resume does not evidence, do not claim it, do not imply
   it, and do not promise to learn it. Silence is better than invention.
2. Address what this advert actually asks for. Quote or name at most two
   specifics from it so it is obvious the letter was not mass-produced.
3. Prefer a number the resume already contains over an adjective.
4. 180 to 250 words. Four short paragraphs at most.
5. Output the letter body ONLY: no subject line, no salutation, no sign-off,
   no commentary. Those are added on send.

If the resume genuinely does not support an application to this advert, say so
in one sentence instead of writing a letter, beginning with "NO STRONG MATCH:".
"""


def build_prompt(row, requirements=()) -> str:
    """The user turn. `row` is a `jobs` row; `requirements` are its asks.

    The resume goes in the system block (see `build_system`), not here, so the
    per-job turn stays small and the expensive half stays cacheable across a
    `--top N` batch.
    """
    advert = (row["description"] or "").strip()
    asks = [
        "  - {}{}{}".format(
            r["kind"],
            " (MANDATORY)" if r["mandatory"] else "",
            ": " + r["detail"] if r["detail"] else "",
        )
        for r in requirements
    ]
    parts = [
        "ADVERT",
        f"Title: {row['title']}",
        f"Company: {row['company'] or 'not stated'}",
        f"Source: {row['source']}",
        f"URL: {row['url']}",
        "",
        advert if advert else "(no advert text was stored for this job)",
    ]
    if asks:
        parts += ["", "THIS ADVERT EXPLICITLY ASKS THE APPLICANT TO PRODUCE", *asks]
    if row["why"]:
        parts += [
            "",
            "WHY IT SCORED AS IT DID (the ranker's note, for context only)",
            row["why"],
        ]
    return "\n".join(parts)


def build_system(profile) -> list[dict]:
    """Cached prefix: rules plus the resume.

    Byte-identical across every job in one invocation, so drafting five
    letters pays for the resume once. Same discipline as `scorer.build_system`
    and for the same reason.
    """
    body = "\n\n".join(
        [
            INSTRUCTIONS,
            VOICE,
            "CANDIDATE\n"
            f"Name: {profile.name}\n"
            f"Based in: {profile.based_in} (UTC{profile.utc_offset:+d})",
            "CANDIDATE RESUME\n" + profile.resume,
        ]
    )
    return [{"type": "text", "text": body, "cache_control": {"type": "ephemeral"}}]


def render(row, letter: str, blockers: list[str], manual_steps=()) -> str:
    """The file a human opens. Draft banner first, warnings before the text.

    `manual_steps` are things this posting demands that take work before the
    application can go out at all. They never affected the score and must not
    read as if they did, but they belong at the top: the letter is useless if
    the video it asks for was never recorded.
    """
    score = row["score"] if row["score"] is not None else "--"
    lines = [
        f"# Draft cover letter: {row['title']}",
        "",
        f"**{row['company'] or 'Company not stated'}**, score {score}, "
        f"[{row['source']}]({row['url']})",
        "",
        "> WARNING. This is a draft and has not been sent. Read every sentence"
        " before you use it. The model was told not to claim anything the resume"
        " does not evidence, but that instruction is not a guarantee.",
        "",
    ]
    if blockers:
        lines += [
            "> BLOCKED. This posting asks for something listed as `cannot_provide`:"
            f" {', '.join(blockers)}. Decide whether to apply at all before spending"
            " time editing this.",
            "",
        ]
    if manual_steps:
        lines += ["## Before applying", ""]
        lines += [f"- [ ] {config.REQUIREMENT_KINDS.get(k, k)}" for k in manual_steps]
        lines += [
            "",
            "_This posting cannot be applied to in one sitting. It did not cost"
            " the job any points._",
            "",
        ]
    lines += ["---", "", letter.strip(), ""]
    return "\n".join(lines)


def draft(client, model, system, row, requirements) -> str:
    response = client.messages.create(
        model=model,
        max_tokens=config.COVER_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": build_prompt(row, requirements)}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(uid: str) -> str:
    """A filesystem-safe, collision-free stem for a uid.

    A uid is `source:source_id`, and on Himalayas the source_id is the
    advert's full URL, so a real uid looks like
    `himalayas:https://himalayas.app/companies/acme/jobs/backend`.

    On Windows that is not merely awkward. `a:b.md` is not a filename: NTFS
    reads the colon as an alternate data stream, so the write appears to
    succeed and the text lands somewhere Explorer cannot show, while a
    URL-shaped uid raises OSError outright. Either way the letter is lost
    AFTER the API call has been paid for.

    So: slugify for the filesystem, then append a short hash of the ORIGINAL
    uid, so two adverts that slugify to the same string cannot overwrite each
    other and the stem can never collide with a Windows reserved device name.
    """
    digest = hashlib.sha256(uid.encode("utf-8")).hexdigest()[:8]
    stem = _UNSAFE.sub("-", uid).strip("-._")[:80].rstrip("-._")
    return f"{stem or 'job'}-{digest}"


def out_path(uid: str) -> pathlib.Path:
    return pathlib.Path(config.COVER_DIR) / f"{slug(uid)}.md"


def row_from(verdict, result) -> dict:
    """Adapt an in-flight (Verdict, result) pair to the shape a draft needs.

    A run drafts BEFORE anything is written to SQLite, so there is no `jobs`
    row to read yet. Drafting after the write would be tidier, but it would
    also mean a job that failed to store silently loses its letter.
    """
    job = verdict.job
    return {
        "uid": job.key,
        "title": job.title,
        "company": job.company,
        "source": job.source,
        "url": job.url,
        "description": job.description,
        "why": result.get("why") or "",
        "score": result.get("score"),
        "blockers": json.dumps(result.get("blockers") or []),
    }


def auto_draft(pairs, profile, api_key, log=print) -> int:
    """Draft letters for the high scorers of a run. Returns how many were written.

    Selection is per board, because a score is only meaningful within the board
    that produced it: 70 is a rare top result on one board and unremarkable on
    another. A single global cap then bounds the spend regardless of how good a
    day it was, and the slots are shared out a board at a time.

    Never raises. Drafting is a convenience layered on a run that has already
    succeeded; it must not be able to fail that run.
    """
    candidates = []
    for verdict, result in pairs:
        score = result.get("score")
        if score is None:
            continue
        try:
            board = profile.board(verdict.job.source)
        except Exception:  # noqa: BLE001 - an unconfigured board just does not draft
            continue
        if score >= board.draft_at:
            candidates.append((score, verdict, result))

    # The cap is shared out round-robin -- each board's best undrafted job, then
    # each board's second, and so on -- never by one list sorted across boards.
    # A global sort silently reintroduces the comparison this whole design
    # exists to prevent: on real history one board averages 20.3 and the other
    # 5.9, so ranking them together hands every slot to the generous board and
    # starves the board where clearing draft_at was the harder thing to do.
    # Within a board the ranking is real, so there it is highest-first.
    by_board: dict[str, list] = {}
    for candidate in candidates:
        if out_path(candidate[1].job.key).exists():
            continue
        by_board.setdefault(candidate[1].job.source, []).append(candidate)
    for rows in by_board.values():
        rows.sort(key=lambda c: -c[0])

    todo = [
        c
        for tier in itertools.zip_longest(*(by_board[b] for b in sorted(by_board)))
        for c in tier
        if c is not None
    ]
    skipped = len(candidates) - len(todo)
    capped = todo[: config.COVER_MAX_PER_RUN]

    if not capped:
        if skipped:
            log(f"[cover] {skipped} qualifying job(s) already drafted; nothing new")
        return 0

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        system = build_system(profile)
    except Exception as exc:  # noqa: BLE001
        log(f"[cover] skipped, could not start the client: {exc}")
        return 0

    pathlib.Path(config.COVER_DIR).mkdir(parents=True, exist_ok=True)
    if len(todo) > len(capped):
        log(f"[cover] {len(todo)} qualify, drafting the top {len(capped)} "
            f"(COVER_MAX_PER_RUN); the rest are available via `py cover.py <uid>`")

    written = 0
    for score, verdict, result in capped:
        row = row_from(verdict, result)
        requirements = result.get("requirements") or []
        try:
            letter = draft(client, config.COVER_MODEL, system, row, requirements)
        except Exception as exc:  # noqa: BLE001 - one failure must not lose the rest
            log(f"[cover] failed {verdict.job.title[:44]}: {type(exc).__name__}: {exc}")
            continue
        target = out_path(row["uid"])
        steps = live_manual_steps(requirements, profile)
        target.write_text(
            render(row, letter, live_blockers(row, profile), steps), encoding="utf-8"
        )
        written += 1
        flag = "  needs: " + ", ".join(steps) if steps else ""
        log(f"[cover] {score}  {verdict.job.title[:44]}  -> {target.name}{flag}")
    return written


def live_manual_steps(requirements, profile) -> list[str]:
    """What this posting demands that costs work but not points.

    Re-derived from the stored requirement rows against the CURRENT profile,
    never stored on the job. Editing needs_manual_step therefore re-flags the
    whole history for free, exactly as editing cannot_provide does.
    """
    return [r["kind"] for r in requirements if r["kind"] in profile.needs_manual_step]


def live_blockers(row, profile) -> list[str]:
    """Blockers stored on the row that the CURRENT profile still declares.

    Re-intersected rather than trusted: `blockers` was denormalised at write
    time, so a key dropped from `cannot_provide` since must stop warning.
    """
    return [k for k in json.loads(row["blockers"] or "[]") if k in profile.cannot_provide]


def log(message: str) -> None:
    """Progress to stderr, results to stdout. Same split as main.py."""
    print(message, file=sys.stderr)


def load_api_key() -> str:
    """Same resolution order as main.py: environment first, then .env."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    env = pathlib.Path(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def _collect(store, args) -> list:
    """Resolve the requested jobs, or raise LookupError with an actionable message.

    Deduplicated on uid throughout: a repeated argument, or a named uid that
    also appears in the top N, must not be drafted and billed twice.
    """
    rows: list = []
    seen: set[str] = set()
    for uid in args.uids:
        row = store.get_job(uid)
        if row is None:
            raise LookupError(
                f"no stored job matches {uid!r}. Run `py main.py` first, or check"
                " the uid in the digest."
            )
        if row["uid"] not in seen:
            seen.add(row["uid"])
            rows.append(row)
    if args.top:
        # Over-fetch, then trim. Asking SQLite for exactly N and then removing
        # the overlap with named uids would quietly return fewer than N.
        pool = store.recent(limit=args.top + len(seen), min_score=args.min_score)
        for row in pool:
            if len(rows) - len(args.uids) >= args.top:
                break
            if row["uid"] not in seen:
                seen.add(row["uid"])
                rows.append(row)
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("uids", nargs="*", help="job uid(s); a unique prefix works")
    parser.add_argument(
        "--top", type=int, metavar="N", help="draft for the N highest-scoring stored jobs"
    )
    parser.add_argument(
        "--min-score", type=int, default=0, help="with --top, ignore jobs below this score"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the prompt and exit; calls nothing, costs nothing",
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing draft instead of skipping"
    )
    args = parser.parse_args(argv)

    if not args.uids and not args.top:
        parser.error("give at least one uid, or --top N")
    if args.top is not None and args.top < 1:
        # SQLite reads a negative LIMIT as unbounded, so an unchecked --top -1
        # would draft, and bill for, every job in the store.
        parser.error("--top must be 1 or more")

    from store import Store

    profile = targeting.load()
    with Store() as store:
        try:
            rows = _collect(store, args)
        except LookupError as exc:
            log(f"[error] {exc}")
            return 1

        if not rows:
            log("[error] nothing to draft: no stored job matched. Run `py main.py`"
                " first, or lower --min-score.")
            return 1

        missing = [r for r in rows if not (r["description"] or "").strip()]
        if missing:
            # Predates the `description` column, or was stored under --no-llm.
            # `--rescore` only helps for a listing still inside the source's
            # window: an advert that has aged out cannot be recovered, and
            # saying so here is kinder than sending someone to a command that
            # will silently change nothing.
            log(f"[warn] {len(missing)} job(s) have no stored advert text and will"
                " be skipped. `py main.py --rescore` backfills any that are still"
                " live on the source; older ones cannot be recovered.")
            rows = [r for r in rows if (r["description"] or "").strip()]
            if not rows:
                log("[error] none of the requested jobs have advert text stored.")
                return 1

        if args.dry_run:
            for row in rows:
                print(build_prompt(row, store.requirements_for(row["uid"])))
                print("\n" + "=" * 72 + "\n")
            return 0

        api_key = load_api_key()
        if not api_key:
            log("[error] no ANTHROPIC_API_KEY (env or .env).")
            return 1

        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        system = build_system(profile)
        pathlib.Path(config.COVER_DIR).mkdir(parents=True, exist_ok=True)

        written, failed = 0, 0
        for row in rows:
            target = out_path(row["uid"])
            if target.exists() and not args.force:
                log(f"[skip] {target.name} already exists (use --force to redraft)")
                continue
            requirements = store.requirements_for(row["uid"])
            try:
                letter = draft(client, config.COVER_MODEL, system, row, requirements)
            except anthropic.APIError as exc:
                # One bad job must not discard the drafts already written, and
                # a traceback tells the caller nothing about how far it got.
                failed += 1
                log(f"[fail] {row['title'][:48]}: {type(exc).__name__}: {exc}")
                continue
            blockers = live_blockers(row, profile)
            steps = live_manual_steps(requirements, profile)
            target.write_text(render(row, letter, blockers, steps), encoding="utf-8")
            written += 1
            score = row["score"] if row["score"] is not None else "--"
            flag = "  BLOCKED: " + ", ".join(blockers) if blockers else ""
            log(f"[ok] {target.name}  ({score})  {row['title'][:48]}{flag}")
        log(f"{written} draft(s) written to {config.COVER_DIR}/. Review before sending.")
        if failed:
            log(f"{failed} job(s) failed; re-run to retry just those.")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
