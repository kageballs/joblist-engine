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
    py cover.py --backfill       every stored job that qualifies and has no
                                 letter yet, across the whole store
    py cover.py <uid> --dry-run  print the prompt, call nothing

A job qualifies when its score clears its own board's `draft_at`, OR when the
advert asks for a cover letter and the score clears the board's lower
`draft_if_asked_at`. Both bars are per board, because a score means nothing
across boards. `--backfill --dry-run` lists what would be drafted, and why,
without calling anything.

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


# Does the advert itself ask for a cover letter?
#
# Read off the stored text with a regex and no model call. The phrasing is
# formulaic, the check costs nothing, and keeping it deterministic means the
# rule can be changed and the whole stored history re-priced for free -- the
# same discipline as `report.py --reapply` (see docs/determinism.md).
_LETTER_ASK = re.compile(
    r"(?:cover|covering|application|motivation|motivational)\s+letters?"
    r"|letters?\s+of\s+(?:application|interest|motivation)",
    re.IGNORECASE,
)

# "No cover letter needed" and "cover letters are not required" are common, and
# both mean the opposite of a match. The negation can sit on either side of the
# phrase, so both sides are examined -- but only within the same clause, since
# a "not" belonging to another sentence says nothing about this one.
_NEG_BEFORE = re.compile(
    r"\b(?:no|not|without|dont|don't|do\s+not|does\s+not|skip|omit|avoid|never|"
    r"rather\s+than|instead\s+of)\b[^.;:!?]{0,60}$",
    re.IGNORECASE,
)
_NEG_AFTER = re.compile(
    r"^[^.;:!?]{0,60}\b(?:not\s+(?:required|needed|necessary)|unnecessary|"
    r"un-?needed|optional|is\s+not|are\s+not|isn't|aren't)\b",
    re.IGNORECASE,
)


def asks_for_cover_letter(text) -> bool:
    """True when the advert asks the applicant to send a letter.

    A posting that asks for one is a different case from a posting that scores
    well: the application is incomplete without it, however good the match. So
    this is a trigger in its own right, not a tiebreak.

    Known limitation, tested rather than hidden: a double negative such as
    "applications without a cover letter will not be read" is read as a
    refusal and missed. The direction of that error is the point -- a miss
    costs an auto-draft that `py cover.py <uid>` can still produce on request,
    while a false positive spends a long call writing a document the employer
    said not to send.
    """
    body = (text or "").strip()
    if not body:
        return False
    for match in _LETTER_ASK.finditer(body):
        before = body[max(0, match.start() - 80):match.start()]
        after = body[match.end():match.end() + 80]
        if _NEG_BEFORE.search(before) or _NEG_AFTER.search(after):
            continue
        return True
    return False


def draft_reason(score, board, description) -> str | None:
    """Why this job should get a letter, or None. The single place that decides.

    Two independent triggers, and the independence is the point:

      * `score >= board.draft_at` -- the job is good enough to be worth the
        call, judged against its own board and nobody else's.
      * the advert asks for a letter, and the job clears the board's lower
        `draft_if_asked_at` bar -- the employer has said the application is
        incomplete without one, so a merely decent match still needs it.

    The second bar exists so the ask cannot drag the floor to zero. An advert
    scoring 20 that demands a cover letter is not an application anyone is
    going to send, and drafting it would spend a long call to produce a file
    nobody opens.
    """
    if score is None:
        return None
    if score >= board.draft_at:
        return f"score {score} at or above {board.name} draft_at {board.draft_at}"
    if score >= board.draft_if_asked_at and asks_for_cover_letter(description):
        return (
            f"the advert asks for a cover letter, and score {score} clears "
            f"{board.name} draft_if_asked_at {board.draft_if_asked_at}"
        )
    return None


def select_backfill(rows, profile) -> list[tuple]:
    """Stored jobs that should have a letter and do not. Returns (row, reason).

    `auto_draft` only ever sees the jobs of one run, so a job that qualified on
    a day the cap was already spent never gets a second look. This walks the
    whole store instead, which is what makes the backlog reachable at all.

    Repostings are collapsed the same way `auto_draft` collapses them, and for
    the same reason -- but with one extra step that matters here. A board can
    list one advert under two uids, and the letter may sit on either of them,
    so the check is "does any uid with this signature already have a file",
    not "does this uid". Himalayas' Accounting Automation Engineer is exactly
    this case: two uids, same 4215-character description, one drafted. Asking
    per uid would draft the twin and pay twice for one job.
    """
    drafted: set[tuple] = set()
    qualified: list[tuple] = []

    for row in rows:
        try:
            board = profile.board(row["source"])
        except Exception:  # noqa: BLE001 - an unconfigured board just does not draft
            continue
        signature = (
            row["source"],
            (row["title"] or "").strip().lower(),
            (row["company"] or "").strip().lower(),
        )
        if out_path(row["uid"]).exists():
            drafted.add(signature)
            continue
        reason = draft_reason(row["score"], board, row["description"])
        if reason:
            qualified.append((signature, row, reason))

    picked: list[tuple] = []
    seen: set[tuple] = set()
    for signature, row, reason in qualified:
        if signature in drafted or signature in seen:
            continue
        seen.add(signature)
        picked.append((row, reason))
    return picked


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
    """One letter. Thinking is explicitly OFF, and that is load-bearing.

    Measured 2026-09-04 on onlinejobs:1722982: with the model's default
    thinking left on, the call came back `stop_reason: max_tokens` having
    spent all 1200 output tokens inside a single `thinking` block, emitting no
    text at all. The caller saw an empty string and correctly refused to write
    a file -- so three adverts "returned nothing" that day, and not one of them
    was a refusal. With thinking disabled the same advert answered in 74
    tokens.

    Drafting is a writing task, not a reasoning one. The budget belongs to the
    letter.
    """
    response = client.messages.create(
        model=model,
        max_tokens=config.COVER_MAX_TOKENS,
        system=system,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": build_prompt(row, requirements)}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    # An empty answer that stopped at the token ceiling is a budget bug, not a
    # judgement. Say which one it is, or the next person spends an afternoon
    # reading the prompt for a fault that is in the number above it.
    if not text.strip() and response.stop_reason == "max_tokens":
        raise RuntimeError(
            f"no letter and stopped at max_tokens ({config.COVER_MAX_TOKENS}): "
            f"the budget was consumed before any text was written"
        )
    return text


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
        if draft_reason(score, board, verdict.job.description):
            candidates.append((score, verdict, result))

    # The cap is shared out round-robin -- each board's best undrafted job, then
    # each board's second, and so on -- never by one list sorted across boards.
    # A global sort silently reintroduces the comparison this whole design
    # exists to prevent: on real history one board averages 20.3 and the other
    # 5.9, so ranking them together hands every slot to the generous board and
    # starves the board where clearing draft_at was the harder thing to do.
    # Within a board the ranking is real, so there it is highest-first.
    # Collapse repostings first. A board can list the same advert under two
    # ids -- Himalayas served `accounting-automation-engineer-3984977259` and
    # `-3943339358`, same company, same 4215-character description, same score
    # -- and uid dedupe cannot see it because the uids genuinely differ. Left
    # alone it buys two API calls and two slots for one job, which on a cap of
    # five is most of the run's budget.
    best: dict[tuple, tuple] = {}
    for candidate in candidates:
        if out_path(candidate[1].job.key).exists():
            continue
        job = candidate[1].job
        signature = (job.source, job.title.strip().lower(), (job.company or "").strip().lower())
        if signature not in best or candidate[0] > best[signature][0]:
            best[signature] = candidate

    by_board: dict[str, list] = {}
    for candidate in best.values():
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
        if not letter.strip():
            # Writing the banner with no letter under it is worse than writing
            # nothing at all. The file's existence is what marks a job already
            # drafted, so an empty one would permanently skip this job on every
            # future run -- the advert would never get a second attempt.
            log(f"[cover] empty response for {verdict.job.title[:44]}, not written")
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


def _collect(store, args, profile) -> list:
    """Resolve the requested jobs, or raise LookupError with an actionable message.

    Deduplicated on uid throughout: a repeated argument, or a named uid that
    also appears in the top N, must not be drafted and billed twice.
    """
    rows: list = []
    seen: set[str] = set()

    if getattr(args, "backfill", False):
        # No practical ceiling: the point of a backfill is that nothing
        # qualifying is left behind. `--limit` is the bound, and it is applied
        # after ranking so the cap always keeps the best jobs, not the first
        # ones SQLite happened to return.
        picked = select_backfill(store.recent(limit=1_000_000), profile)
        if args.limit:
            dropped = max(0, len(picked) - args.limit)
            picked = picked[: args.limit]
            if dropped:
                log(f"[cover] {dropped} more qualify beyond --limit {args.limit}")
        log(f"[cover] backfill: {len(picked)} job(s) qualify with no letter yet")
        for row, reason in picked:
            log(f"[cover]   {row['score'] if row['score'] is not None else '--':>3}"
                f"  {row['title'][:46]}  ({reason})")
            if row["uid"] not in seen:
                seen.add(row["uid"])
                rows.append(row)

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
        "--backfill",
        action="store_true",
        help="draft for every stored job that qualifies and has no letter yet",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="with --backfill, draft at most N (highest scores first)",
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

    if not args.uids and not args.top and not args.backfill:
        parser.error("give at least one uid, or --top N, or --backfill")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be 1 or more")
    if args.top is not None and args.top < 1:
        # SQLite reads a negative LIMIT as unbounded, so an unchecked --top -1
        # would draft, and bill for, every job in the store.
        parser.error("--top must be 1 or more")

    from store import Store

    profile = targeting.load()
    with Store() as store:
        try:
            rows = _collect(store, args, profile)
        except LookupError as exc:
            log(f"[error] {exc}")
            return 1

        if not rows:
            if getattr(args, "backfill", False):
                log("[cover] nothing to draft: every qualifying job already has a"
                    " letter. Lower a board's `draft_at` or `draft_if_asked_at`"
                    " in profile.yaml to widen it.")
                return 0
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
            if not letter.strip():
                # Same reason as auto_draft: the file existing is what marks a
                # job done, so an empty one is a permanent skip, not a retry.
                failed += 1
                log(f"[fail] {row['title'][:48]}: empty response, nothing written")
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
