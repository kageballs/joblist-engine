#!/usr/bin/env python3
"""Add one job you found yourself — by URL, or by pasting the advert.

    py add.py <url>                     fetch that page, parse, score, store
    py add.py <url> --text-file ad.txt  advert body from a file, no fetch
    py add.py <url> --paste             advert body from stdin, no fetch
    py add.py <url> --dry-run           print what would happen, call nothing

The pipeline in `main.py` answers "what is on the boards today". This answers a
different question: "I found this one myself — put it through the same rubric."
A referral, something on a board with no feed, a link from a friend. It reaches
the digest, the dashboard and the cover-letter drafter exactly like a fetched
job, so a posting you found by hand is not a second-class citizen in your own
triage.

**Deliberately one job per invocation.** No URL list, no input file of links, no
following links found on the page, and `main.py` never calls this. That is a
design boundary, not an oversight: this is a tool for reading a page you chose
to open, and the moment it takes a list it becomes a crawler, which is a
different thing that this repo has twice decided not to build. If a board is
worth having in bulk it gets a real source module in `sources/` with a measured
capability flag, or it does not get read at all.

Some sites will not serve a plain HTTP client — `ph.jobstreet.com` returns 403
with a Cloudflare challenge on the first request, measured 2026-09-07. That is
not a bug to route around here. Use `--text-file` or `--paste`: you already have
the page open, so copy the advert out of it. Nothing is fetched in that mode.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime

import blockers as blockers_mod
import config
import cover
import filters
import targeting
from sources.base import Job, strip_html
from store import Store

BOARD = "manual"


def log(message: str) -> None:
    print(message, file=sys.stderr)


class Manual:
    """The 'board' a hand-added job belongs to.

    Both capability flags are False and that is the honest answer, not a
    conservative default. An arbitrary page's silence about hiring regions
    proves nothing — the same reasoning that makes We Work Remotely and
    ph.indeed.com untrustworthy on region — so `filters.evaluate` marks an empty
    restriction list `region_unverified` and `scorer` is told the silence is not
    evidence. Pay is just as often prose here as a field.
    """

    name = BOARD
    regions_authoritative = False
    salary_authoritative = False
    publishes_expiry = False


# --- fetching ----------------------------------------------------------------

CHALLENGE = ("just a moment", "cf-browser-verification", "challenge-platform",
             "enable javascript and cookies", "security check")


class Unfetchable(RuntimeError):
    """The page cannot be read by a plain client. Says what to do instead."""


def fetch_page(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(url, headers={
        "User-Agent": config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        if exc.code in (401, 403, 429):
            raise Unfetchable(
                f"{url} answered HTTP {exc.code}. This site does not serve a plain "
                f"client. Open it in your browser, copy the advert text, and run:\n"
                f"    py add.py {url!r} --text-file ad.txt") from exc
        raise Unfetchable(f"{url} answered HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001 - one job, one clear message
        raise Unfetchable(f"could not fetch {url}: {exc}") from exc

    html = body.decode("utf-8", "replace")
    lowered = html[:4000].lower()
    if any(marker in lowered for marker in CHALLENGE):
        raise Unfetchable(
            f"{url} returned a bot challenge rather than the advert. Open it in "
            f"your browser, copy the advert text, and run:\n"
            f"    py add.py {url!r} --text-file ad.txt")
    return html


# --- parsing -----------------------------------------------------------------

_LD = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                 re.I | re.S)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_OG = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I)


def _walk(node):
    """Yield every dict in a JSON tree, so @graph and arrays are both handled."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def find_job_posting(html: str) -> dict | None:
    """The schema.org JobPosting on the page, if there is one.

    Parse this rather than the DOM. Every board renders its markup differently
    and rewrites it without warning — sources/indeed.py exists because the only
    handle left on that site's description was a hashed CSS-in-JS class — but
    JobPosting is a published contract that boards maintain for Google Jobs.

    Returns None rather than raising on unusable input, the same rule every
    parse function in `sources/` follows.
    """
    for block in _LD.findall(html or ""):
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        for node in _walk(data):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if any(str(t).lower() == "jobposting" for t in types):
                return node
    return None


def _iso(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _text(value) -> str:
    """A schema.org field that may be a string, a dict, or a list of either."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("value") or "")
    if isinstance(value, list):
        return ", ".join(t for t in (_text(v) for v in value) if t)
    return ""


def _regions(posting: dict) -> tuple[str, ...]:
    """Hiring restrictions, when the page states them.

    An empty tuple here means "the page did not say", never "worldwide" —
    `Manual.regions_authoritative` is False precisely so the funnel treats this
    as unverified rather than as the good case.
    """
    names = []
    for key in ("applicantLocationRequirements", "jobLocation"):
        value = posting.get(key)
        for node in _walk(value):
            name = _text(node.get("name") or node.get("addressCountry") or "")
            if name and name not in names:
                names.append(name)
    return tuple(names)


def _salary(posting: dict):
    """(min, max, period, currency) from baseSalary, or all None.

    Never guesses a period. `annual_usd_max` reads None as "unknown", which is
    the correct tri-state answer for a field the page did not state, and is very
    much better than inventing "annual" and rejecting a real job on it.
    """
    base = posting.get("baseSalary")
    if not isinstance(base, dict):
        return None, None, None, None
    currency = _text(base.get("currency") or base.get("salaryCurrency")) or None
    value = base.get("value")
    if not isinstance(value, dict):
        return None, None, None, currency

    def num(*keys):
        for key in keys:
            raw = value.get(key)
            if isinstance(raw, (int, float)) and raw > 0:
                return int(raw)
            if isinstance(raw, str):
                try:
                    return int(float(raw.replace(",", "")))
                except ValueError:
                    continue
        return None

    low, high = num("minValue", "value"), num("maxValue", "value")
    unit = _text(value.get("unitText")).lower()
    period = {"hour": "hourly", "day": None, "week": None,
              "month": "monthly", "year": "annual"}.get(unit)
    return low, (high or low), period, (currency.upper() if currency else None)


def job_from_posting(posting: dict, url: str) -> Job:
    low, high, period, currency = _salary(posting)
    description = strip_html(_text(posting.get("description")))
    return Job(
        source=BOARD,
        source_id=url,
        title=_text(posting.get("title")) or "(untitled)",
        company=_text(posting.get("hiringOrganization")) or "",
        url=url,
        # No datePosted means you found it today, which is the only honest
        # answer available and keeps it out of the stale bucket immediately.
        posted=_iso(posting.get("datePosted")) or datetime.now(UTC),
        description=description,
        excerpt=description[:280],
        location_restrictions=_regions(posting),
        salary_min=low, salary_max=high, salary_period=period, currency=currency,
        expires=_iso(posting.get("validThrough")),
        raw={"jsonld": True},
    )


def job_from_text(url: str, body: str, title: str = "", company: str = "") -> Job:
    """A job built from advert text you pasted. No fetch, no parsing of a page.

    Title falls back to the first non-empty line, which is what an advert copied
    out of a browser almost always starts with.
    """
    body = (body or "").strip()
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    return Job(
        source=BOARD,
        source_id=url,
        title=title or (lines[0][:120] if lines else "(untitled)"),
        company=company,
        url=url,
        posted=datetime.now(UTC),
        description=body,
        excerpt=body[:280],
        raw={"pasted": True},
    )


def build_job(url: str, body: str | None, title: str, company: str) -> tuple[Job, str]:
    """The job, and one line saying where it came from."""
    if body is not None:
        return job_from_text(url, body, title, company), "pasted text"
    html = fetch_page(url)
    posting = find_job_posting(html)
    if posting:
        job = job_from_posting(posting, url)
        if title:
            job = replace_field(job, "title", title)
        if company:
            job = replace_field(job, "company", company)
        return job, "schema.org JobPosting"
    # No structured data. Fall back to the page's own text rather than refusing:
    # the model reads prose perfectly well, and a title is recoverable.
    text = strip_html(re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", html))
    guess = _OG.search(html) or _TITLE.search(html)
    return job_from_text(url, text, title or (guess.group(1).strip() if guess else ""),
                         company), "page text (no JobPosting found)"


def replace_field(job: Job, field: str, value):
    """Job is frozen, so a --title/--company override rebuilds it."""
    return dataclasses.replace(job, **{field: value})


# --- the run -----------------------------------------------------------------

def advise_not_veto(verdict) -> str:
    """Let a hand-added job through the funnel, and return what it objected to.

    Here the funnel ADVISES; it does not veto. Everywhere else in this repo a
    rejection is final and correct, because there the tool is choosing among
    thousands of listings nobody has looked at. This path is the opposite
    situation: a person has already read the posting and decided it was worth
    adding, so silently dropping it would be the tool overruling the only party
    that has actually seen the job.

    The objection is not thrown away — it is returned so the caller can print it
    and so it lands in the digest line. A region cut on a manual add is usually
    real information ("this one says US-only"), it just is not a decision this
    code gets to make.

    Mutates rather than rebuilding because `Verdict` is not frozen and the
    scorer needs the same instance the funnel annotated.
    """
    if verdict.passed:
        return ""
    objection = f"{verdict.rejected_by}: {verdict.reason}"
    verdict.rejected_by = None
    verdict.reason = ""
    return objection


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="the posting's URL — its identity, and how you apply")
    parser.add_argument("--text-file", help="advert body from this file instead of fetching")
    parser.add_argument("--paste", action="store_true", help="advert body from stdin")
    parser.add_argument("--title", default="", help="override the parsed title")
    parser.add_argument("--company", default="", help="override the parsed company")
    parser.add_argument("--profile", default=config.PROFILE_PATH)
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be stored; no API call, nothing persisted")
    parser.add_argument("--no-draft", action="store_true", help="skip the cover letter")
    parser.add_argument("--fast", action="store_true", help="score with the cheaper model")
    args = parser.parse_args(argv)

    try:
        profile = targeting.load(args.profile)
        profile.require_boards([BOARD])
    except targeting.ProfileError as exc:
        log(f"[error] {exc}")
        log(f"[error] add a `{BOARD}:` block under `boards:` in {args.profile}. "
            "Every board declares its own policy — see CLAUDE.md.")
        return 1

    body = None
    if args.text_file:
        body = open(args.text_file, encoding="utf-8").read()
    elif args.paste:
        log("[add] reading the advert from stdin, end with Ctrl-Z (Windows) or Ctrl-D")
        body = sys.stdin.read()

    try:
        job, origin = build_job(args.url, body, args.title, args.company)
    except Unfetchable as exc:
        log(f"[error] {exc}")
        return 1

    if not job.description.strip():
        log("[error] no advert text — nothing to score. Use --text-file or --paste.")
        return 1

    log(f"[add] {origin}: {job.title!r} @ {job.company or 'unknown company'} "
        f"({len(job.description)} chars)")

    now = datetime.now(UTC)
    verdict = filters.evaluate(job, profile, now, Manual())
    overridden = advise_not_veto(verdict)
    if overridden:
        log(f"[warn] the funnel would have rejected this ({overridden}) — "
            "keeping it anyway, because you added it on purpose")

    if args.dry_run:
        log("[add] dry-run: nothing scored, nothing persisted")
        print(json.dumps({
            "uid": job.key, "title": job.title, "company": job.company,
            "url": job.url, "source": job.source, "origin": origin,
            "posted": job.posted.isoformat(),
            "regions": list(job.location_restrictions),
            "salary": [job.salary_min, job.salary_max, job.salary_period, job.currency],
            "salary_signal": verdict.salary_signal,
            "would_have_been_rejected": overridden or None,
            "description_chars": len(job.description),
        }, indent=2))
        return 0

    # Reuse main.py's reader rather than writing a third .env parser. push.py
    # already has its own; a fourth would be three chances to disagree about
    # where the key lives.
    from main import load_api_key

    api_key = load_api_key()
    if not api_key:
        log("[error] no ANTHROPIC_API_KEY (env or .env)")
        return 1

    import scorer
    model = config.FAST_MODEL if args.fast else config.SCORING_MODEL
    results = blockers_mod.apply(scorer.score([verdict], profile, api_key, model=model), profile)
    result = results[0]

    store = Store()
    run_id = store.start_run()
    store.record_job(
        verdict.job, run_id, verdict.salary_signal,
        score=result.get("score"), verdict=result.get("verdict"),
        why=result.get("why"), cv_variant=result.get("cv_variant"),
        score_raw=result.get("score_raw"), blockers=result.get("blockers"),
        requirements=result.get("requirements"),
    )
    store.commit()
    store.finish_run(run_id, ok=True, fetched=1, scored=1,
                     funnel="1 added by hand", model=model)

    score = result.get("score")
    print(f"{score if score is not None else '--':>3}  {job.title}")
    if result.get("why"):
        print(f"     {result['why']}")
    if result.get("blockers"):
        print(f"     BEFORE APPLYING: {', '.join(result['blockers'])}")
    if overridden:
        print(f"     note: the funnel would have cut this — {overridden}")

    if not args.no_draft:
        drafted = cover.auto_draft([(verdict, result)], profile, api_key, log=log)
        if drafted:
            print(f"     letter drafted in {config.COVER_DIR}/")
        else:
            board = profile.board(BOARD)
            print(f"     no letter: score is under {BOARD} draft_at "
                  f"{board.draft_at}. Force one with: py cover.py {job.key!r}")

    store.close()
    print("\nstored. `py push.py` to put it on the dashboard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
