"""Indeed (ph.indeed.com) — read from capture files, never the network.

Indeed sits behind Cloudflare. A plain `requests` GET, with this repo's own
user agent or with an ordinary desktop Chrome one, is answered by a challenge
page on the very first request — the check is on the TLS/JS fingerprint, not
the headers, so there is no header tuning that gets past it (measured
2026-09-03, docs/indeed-capture.md). A real browser session walks straight
through, so that is what produces the data: a person or agent drives Chrome,
evaluates a scrubbing snippet in the page, and writes the result to
`data/captures/indeed-<date>.json`.

This module reads only that file off disk. It makes no HTTP requests, ever —
`main.py` cannot drive a browser, and a scheduled run hitting Indeed's search
directly is exactly the "regular machine-like pattern" that got the capturing
session itself rate-limited after ~20 requests in a few minutes
(docs/indeed-capture.md). The split also buys the usual benefit: this parser
is testable against a fixture with no network at all, same as onlinejobs.

A capture is a photograph, not a feed — running `py main.py` daily does not
refresh Indeed even once. Someone has to take a new capture by hand.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections.abc import Iterator
from datetime import UTC, datetime

import config
from sources.base import Job, strip_html

# YEARLY/MONTHLY/HOURLY is Indeed's own vocabulary (see fixtures/indeed.json);
# Job.salary_period expects the lowercase form annual_usd_max() multiplies by.
_PERIOD_MAP = {"YEARLY": "annual", "MONTHLY": "monthly", "HOURLY": "hourly"}

# Indeed states the country as an ISO code on the card. Only codes we can name
# confidently are mapped: an unrecognised code must yield an EMPTY tuple rather
# than a guess, because in this model a populated tuple is a hard eligibility
# gate and a wrong country silently rejects a job the candidate could take.
_COUNTRY_NAMES = {"PH": "Philippines", "US": "United States"}


def _regions(country) -> tuple[str, ...]:
    name = _COUNTRY_NAMES.get((country or "").strip().upper())
    return (name,) if name else ()


def _iso(value) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating the trailing "Z" Indeed sends.

    `datetime.fromisoformat` does not accept a bare "Z" on every Python this
    repo might run on, so it is swapped for the offset it means before parsing
    rather than assumed away.
    """
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _epoch_ms(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _posted(row: dict) -> datetime | None:
    """`date_posted` (ISO) is preferred; `pub_date` (epoch ms) is the fallback.

    A row that was never hydrated after capture carries neither — see the
    fixture's "Never Hydrated Example" — and must be skipped rather than
    guessed at.
    """
    return _iso(row.get("date_posted")) or _epoch_ms(row.get("pub_date"))


def _currency(salary_text: str | None) -> str | None:
    """Infer currency from the stated text, never from the site.

    ph.indeed.com is a PH site, but the fixture carries a genuine USD hourly
    row precisely to catch a parser that assumes PHP because of where the
    listing was posted rather than what it says.
    """
    if not salary_text:
        return None
    if "$" in salary_text:
        return "USD"
    if re.search(r"php|₱|\bpesos?\b", salary_text, re.I):
        return "PHP"
    return None


def _usd(amount, rate: float) -> int | None:
    if not amount:
        return None
    try:
        return int(round(float(amount) * rate))
    except (TypeError, ValueError):
        return None


def parse_salary(row: dict) -> tuple[int | None, int | None, str | None, str | None]:
    """A capture row's stated pay -> (min, max, period, currency) in USD.

    `Job.annual_usd_max()` returns None for any currency that is not USD, so a
    PHP figure passed through unconverted would silently become "unknown".
    Converted here with `config.FX_TO_USD`, exactly as sources/onlinejobs.py
    does. An unrecognised period or currency yields no salary at all rather
    than a guessed one — better absent than wrong.
    """
    salary_max = row.get("salary_max")
    if not salary_max:
        return None, None, None, None

    period = _PERIOD_MAP.get(str(row.get("salary_period") or "").upper())
    if period is None:
        return None, None, None, None

    currency = _currency(row.get("salary_text"))
    if currency is None:
        return None, None, None, None

    rate = config.FX_TO_USD.get(currency)
    if rate is None:
        return None, None, None, None

    return _usd(row.get("salary_min"), rate), _usd(salary_max, rate), period, "USD"


def to_job(row: dict, site: str = config.INDEED_SITE) -> Job | None:
    """Map one capture row onto Job. Returns None for anything unusable.

    Never raises. A capture is written by a browser session outside this
    process — its shape can drift the same way Himalayas' undocumented JSON
    did, and a malformed row must degrade to "skipped", not a crashed run.
    """
    try:
        jobkey = row.get("jobkey")
        if not jobkey or row.get("expired"):
            return None

        title = (row.get("title") or "").strip()
        posted = _posted(row)
        if not title or posted is None:
            return None

        description = strip_html(row.get("description"))
        salary_min, salary_max, salary_period, currency = parse_salary(row)

        return Job(
            source="indeed",
            source_id=jobkey,
            title=title,
            company=(row.get("company") or "").strip(),
            url=row.get("url") or f"https://{site}/viewjob?jk={jobkey}",
            posted=posted,
            excerpt=description[:600],
            description=description,
            # Derived from the card's own `country`, not asserted for the whole
            # board. onlinejobs.ph can assert Philippines unconditionally
            # because it is a Philippine board by definition; ph.indeed.com is
            # a localisation of a global site and genuinely carries other
            # countries — the US probe on 2026-09-03 returned `country: "US"`
            # rows from the same model.
            #
            # When the country is unknown the tuple is left EMPTY on purpose,
            # which is what makes `regions_authoritative = False` load-bearing
            # rather than decorative: filters.py then marks the verdict
            # region_unverified and the scorer is told the silence is not
            # evidence, instead of an asserted country quietly hiding the gap.
            location_restrictions=_regions(row.get("country")),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_period=salary_period,
            currency=currency,
            expires=_iso(row.get("valid_through")),
            raw=row,
        )
    except Exception:  # noqa: BLE001 - a captured row has no contract behind it
        return None


def _load_captures(directory: str, max_age_days: float) -> tuple[list[tuple[str, dict]], int]:
    """Well-formed, fresh capture files in `directory`, newest first.

    Returns the usable captures alongside a count of every file actually read
    off disk (parsed as JSON), whether or not it turned out stale — that count
    is what `Indeed.pages_fetched` reports.

    A missing directory yields nothing rather than raising: it just means no
    capture has been taken yet, not that anything is broken. A malformed file
    is skipped with a warning rather than killing the run, because a capture
    is written by a session outside this process and can be interrupted or
    truncated.
    """
    pattern = os.path.join(directory, "indeed-*.json")
    files_read = 0
    usable: list[tuple[datetime, str, dict]] = []

    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"[warn] indeed: skipping unreadable capture {path}: {exc}", file=sys.stderr)
            continue

        files_read += 1
        captured_at = _iso(payload.get("captured_at"))
        if captured_at is None:
            print(f"[warn] indeed: skipping {path}: missing/invalid captured_at", file=sys.stderr)
            continue

        age_days = (datetime.now(UTC) - captured_at).total_seconds() / 86400
        if age_days > max_age_days:
            print(
                f"[warn] indeed: skipping {path}, captured {age_days:.1f}d ago "
                f"(max {max_age_days:.0f}d) — it is a photograph, not a feed",
                file=sys.stderr,
            )
            continue

        usable.append((captured_at, path, payload))

    usable.sort(key=lambda entry: entry[0], reverse=True)
    return [(path, payload) for _, path, payload in usable], files_read


class Indeed:
    name = "indeed"
    # Measured 2026-09-03 (docs/indeed-capture.md): a ph.indeed.com job page
    # carries no applicantLocationRequirements field at all. An empty
    # restriction list therefore proves nothing about eligibility — it is
    # absence of data, not evidence the role is worldwide — so this cannot be
    # left True the way Himalayas' real field is.
    regions_authoritative = False
    # Measured 2026-09-03: pay is stated on roughly 7% of ph.indeed.com cards,
    # and every sampled one carries `salary_source: EXTRACTION` — Indeed
    # inferred the figure from the advert text, the employer never declared
    # it. Rejecting on an inferred number would discard jobs for Indeed's
    # parsing mistakes rather than the employer's actual pay.
    salary_authoritative = False
    # `valid_through` is a real field and is trustworthy when present
    # (docs/indeed-capture.md).
    publishes_expiry = True

    def __init__(self):
        self.pages_fetched = 0
        self.hit_page_cap = False

    def fetch(self, since: datetime) -> Iterator[Job]:
        """Yield jobs posted at or after `since`, newest capture first.

        No network requests — see the module docstring. `pages_fetched` counts
        capture FILES read, not pages of a listing; `hit_page_cap` stays False
        because there is no pagination to cap, only whatever is on disk.
        """
        self.pages_fetched = 0
        self.hit_page_cap = False

        captures, files_read = _load_captures(config.INDEED_CAPTURE_DIR, config.INDEED_MAX_CAPTURE_AGE_DAYS)
        self.pages_fetched = files_read

        seen: set[str] = set()
        for _path, payload in captures:
            site = payload.get("site") or config.INDEED_SITE
            for row in payload.get("results") or []:
                jobkey = row.get("jobkey")
                if not jobkey or jobkey in seen:
                    continue

                job = to_job(row, site)
                if job is None:
                    # Deliberately NOT marked seen. An unparseable row in the
                    # newest capture must not shadow a good copy of the same
                    # advert in an older one: a capture costs a manual browser
                    # session to make, so losing a job to a single bad row is a
                    # worse trade here than on a board we can simply refetch.
                    continue
                seen.add(jobkey)
                if job.posted < since:
                    continue
                yield job
