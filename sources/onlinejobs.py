"""OnlineJobs.ph — the Philippine domestic board.

The inverse of Himalayas, and it needs saying out loud because it inverts the
architecture's central assumption.

Himalayas pays for itself because `locationRestrictions` and
`timezoneRestrictions` are structured fields that eliminate ~97.7% for zero
tokens. Here BOTH of those stages are inert: every listing on this board is
open to Filipinos, and the profile this tool runs for is based in the Philippines. So
region and timezone reject nothing, and **salary becomes the only load-bearing
filter in the funnel**.

Which would be fine, except OnlineJobs.ph states pay as an unvalidated text box.
Measured over 113 listings on 2026-08-30, that field held 60+ distinct formats:
`$500`, `TBD`, `300usd/month`, `90 / week`, `6$`, `5hrly`, `4 aud/hr`,
`PHP 350 - 500 PER HOUR`, `$4.per hour to start plus bonus`, `?`, `10`.

`Job.annual_usd_max()` cannot read any of that, so without `parse_salary()`
below, every one of these would arrive at the scorer as salary `unknown` --
correct per the tri-state rule, and ruinous, because ~96% of this board pays
under the floor. The normaliser is what makes the source affordable. Do not
remove it and lean on the model instead.

robots.txt allows this path and asks for Crawl-delay: 5. We honour it.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime

import requests
from bs4 import BeautifulSoup

import config
from sources.base import Job, strip_html

# Amounts at or above this, with no period stated, cannot be an hourly rate on
# a Filipino VA board -- $200/hr is $416k/yr. They are monthly. Below it the
# figure is genuinely ambiguous ($80 could be hourly, daily, or a weekly gig
# price), and an ambiguous figure must stay `unknown` so it reaches the scorer,
# which can read the description. Guessing "monthly" there would reject the
# one shape of listing on this board actually worth finding.
BARE_AMOUNT_IS_MONTHLY_AT = 200

HOURS_PER_MONTH = 2080 / 12  # 173.33, consistent with filters.HOURS_PER_YEAR

# Pay quoted per deliverable is a project price, not a rate; there is no
# divisor that turns it into one.
_PIECEWORK = re.compile(
    r"fixed[\s-]*price|per (?:listing|article|piece|video|design|task|project|word)"
    r"|per\s+\d|/\s*(?:listing|article|piece|video|design|task|project|word)",
    re.I,
)
# No leading \b on any of these: people write "5hrly" and "25,000PHP" with no
# separator, and a digit-to-letter transition is not a word boundary, so \b
# would silently miss exactly the compact forms this board is full of.
_PERIODS = (
    (re.compile(r"(?:hrly|hourly|/\s*hr|\bper\s*hr|hrs?\b|/\s*hour|\ban?\s+hour|\bper\s+hour|\bhour(?:ly)?\b)", re.I), "hourly"),
    (re.compile(r"(?:wkly|weekly|/\s*wk|/\s*week|\bper\s+week|\ba\s+week|\bweek\b)", re.I), "weekly"),
    (re.compile(r"(?:monthly|/\s*mo|/\s*month|\bper\s+month|\ba\s+month|\bmonth\b|\bmos\b|\bmo\b)", re.I), "monthly"),
    (re.compile(r"(?:yearly|annually|annual|/\s*yr|/\s*year|\bper\s+year|\ba\s+year|\byear\b|\bp\.?a\.?\b)", re.I), "annual"),
)
_TO_ANNUAL = {"hourly": 2080.0, "weekly": 52.0, "monthly": 12.0, "annual": 1.0}
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?\s*[kK]?")
_JOB_ID = re.compile(r"-(\d+)/?$")


def _currency(text: str) -> str:
    """Which currency the figure is in. USD is the board's default.

    No \\b before the code: "25,000PHP" and "PHP100k" are both real, and a
    digit-to-letter transition is not a word boundary. Matching those as USD
    overstates a PHP wage by ~58x, which turns a $2/hr listing into a pass.
    """
    if re.search(r"php|₱|\bpesos?\b", text, re.I):
        return "PHP"
    if re.search(r"aud|\ba\$", text, re.I):
        return "AUD"
    return "USD"


def _numbers(text: str) -> list[float]:
    out = []
    for token in _NUMBER.findall(text):
        cleaned = token.replace(",", "").strip()
        multiplier = 1000.0 if cleaned[-1:].lower() == "k" else 1.0
        cleaned = cleaned.rstrip("kK").strip()
        try:
            out.append(float(cleaned) * multiplier)
        except ValueError:
            continue
    return out


def parse_salary(text: str | None, job_type: str = "") -> tuple[int | None, int | None]:
    """Free-text pay -> (min, max) in USD per year. (None, None) when unusable.

    Deliberately returns annual USD rather than the source's own units, because
    that is the one shape `Job.annual_usd_max()` will accept without a currency
    or period of its own to interpret.

    Unknown is a real and common answer here ("TBD", "Depending on experience",
    a bare "80"). It must stay distinguishable from zero -- filters.stage_salary
    passes unknown through by design, and collapsing the two would silently
    reject the whole board.
    """
    if not text:
        return None, None
    raw = " ".join(str(text).split())
    if not raw:
        return None, None

    # Normalise the dashes and tildes people write ranges with, so a range
    # reads as two numbers rather than one.
    normalised = raw.replace("–", "-").replace("—", "-").replace("~", "-")

    # Pay quoted per deliverable is never a wage, whatever the employment type.
    if _PIECEWORK.search(normalised):
        return None, None

    # A listing that quotes both currencies ("$1,900-2,400/mo | PHP 120k-150k/mo")
    # is one figure written twice. Split BEFORE sniffing the currency: reading
    # the whole string first sees the PHP and then treats the dollar numbers as
    # pesos too, which is how "$2,400/mo" became a PHP figure in testing.
    segments = [s for s in normalised.split("|") if s.strip()] or [normalised]
    chosen = next((s for s in segments if _currency(s) == "USD" and _numbers(s)), segments[0])

    currency = _currency(chosen)
    amounts = [n for n in _numbers(chosen) if n > 0]
    if not amounts:
        return None, None

    period = None
    for pattern, name in _PERIODS:
        if pattern.search(chosen):
            period = name
            break

    if period is None:
        # A Gig with no stated period is a project price -- "$300" to build a
        # page, not $300 a month. Checked only here, after period detection:
        # a gig that DOES state one ("5hrly", "$100/week") is quoting a real
        # rate, and suppressing those was costing the scorer a look at
        # listings the floor could have rejected for free.
        if job_type.strip().casefold() == "gig":
            return None, None

        # No period stated, so read it the way that is most generous to the
        # listing. That direction is what keeps this safe to reject on: if the
        # BEST reading still lands under the floor, every other reading does
        # too, so the rejection is sound rather than a guess.
        #   < 200 -> hourly. "$80" might really be $80/hr, and that is a job
        #            worth surfacing; "$6" is under the floor either way.
        #   >= 200 -> monthly. $200/hr is $416k/yr on a Filipino VA board, so
        #            hourly is not a reading anyone means here.
        period = "hourly" if max(amounts) < BARE_AMOUNT_IS_MONTHLY_AT else "monthly"

    rate = config.FX_TO_USD.get(currency)
    if rate is None:
        return None, None

    factor = _TO_ANNUAL[period] * rate
    annual = sorted(int(round(a * factor)) for a in amounts)
    return annual[0], annual[-1]


def _job_id(href: str) -> str | None:
    match = _JOB_ID.search((href or "").split("?")[0])
    return match.group(1) if match else None


def _posted(card) -> datetime | None:
    """The card carries local PH time and UTC; take the UTC one.

    `data-temp` is UTC+8 and `data-temp-2` is UTC (verified 2026-08-30: the two
    differ by exactly 8h on every card of a 30-card page). Reading the wrong one
    would shift the watermark by eight hours and silently re-fetch or skip.
    """
    node = card.select_one("[data-temp-2]")
    if node is None:
        return None
    try:
        return datetime.fromisoformat(node["data-temp-2"].strip()).replace(tzinfo=UTC)
    except (ValueError, KeyError, TypeError):
        return None


def to_job(card, base_url: str = "https://www.onlinejobs.ph") -> Job | None:
    """Map one search-result card onto Job. Returns None for anything unusable.

    Never raises. This is scraped markup with no contract behind it at all --
    weaker even than Himalayas' undocumented JSON -- so a class rename upstream
    must degrade to "no jobs" rather than a traceback in a scheduled run.
    """
    try:
        link = card.find_parent("a") or card.select_one("a[href*='/jobseekers/job/']")
        href = (link.get("href") if link else "") or ""
        source_id = _job_id(href)

        title_node = card.select_one("h4")
        title = ""
        job_type = ""
        if title_node is not None:
            badge = title_node.select_one(".badge")
            if badge is not None:
                job_type = badge.get_text(" ", strip=True)
                badge.extract()
            title = title_node.get_text(" ", strip=True)

        posted = _posted(card)
        if not source_id or not title or posted is None:
            return None

        logo = card.select_one("img.jobpost-cat-box-logo")
        company = (logo.get("alt") or "").strip() if logo is not None else ""

        salary_node = card.select_one("dl.row.fs-14 dd.col") or card.select_one("dd.col")
        salary_text = salary_node.get_text(" ", strip=True) if salary_node is not None else ""

        desc_node = card.select_one(".desc")
        description = strip_html(desc_node.decode_contents()) if desc_node is not None else ""
        # The card's description is a truncated teaser ending in "See More".
        # Say so rather than letting the scorer treat it as the whole ad.
        description = re.sub(r"\s*See More\s*$", "", description).strip()

        salary_min, salary_max = parse_salary(salary_text, job_type)

        # Hand the model the pay string verbatim. parse_salary() has to commit
        # to one reading of an ambiguous figure to be able to reject at all --
        # a bare "100" is taken as $100/hr, the most generous reading -- and
        # that same commitment would otherwise let it reach the digest labelled
        # "above floor" with nothing to contradict it. The scorer can only call
        # that out if it can see what the employer actually typed.
        if salary_text:
            description = f"Stated pay: {salary_text}\n\n{description}".strip()

        return Job(
            source="onlinejobs",
            source_id=source_id,
            title=title,
            company=company,
            url=f"{base_url}{href}" if href.startswith("/") else href,
            posted=posted,
            excerpt=description[:600],
            description=description,
            # Not scraped from a field -- asserted. Every listing on this board
            # hires Filipinos, which is exactly what `local_anchor` in the
            # profile is for: it keeps these visible and marked as domestic-rate
            # rather than pretending they are worldwide roles.
            location_restrictions=("Philippines",),
            timezone_restrictions=(),
            salary_min=salary_min,
            salary_max=salary_max,
            # parse_salary() has already done the currency and period work, so
            # these two are fixed. annual_usd_max() then multiplies by 1.
            salary_period="annual" if salary_max else None,
            currency="USD" if salary_max else None,
            categories=(job_type,) if job_type else (),
            raw={"salary_text": salary_text, "job_type": job_type},
        )
    except Exception:  # noqa: BLE001 - scraped markup; drift must not crash a run
        return None


def parse_page(html: str) -> list[Job]:
    """All usable jobs on one search page, in document (newest-first) order."""
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001 - a parser problem is still just "no jobs"
        return []
    jobs = []
    for card in soup.select("div.jobpost-cat-box"):
        job = to_job(card)
        if job is not None:
            jobs.append(job)
    return jobs


def parse_detail(html: str) -> str | None:
    """The full advert body from a job's own page, or None if unusable.

    The search card carries a ~280-character teaser cut off mid-sentence at
    "See More". Everything that decides whether this job is applicable -- the
    must-have list, the equipment demands, the hiring restrictions that
    CLAUDE.md notes live in the boilerplate at the BOTTOM of an ad -- is only
    on this page.
    """
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one("#job-description") or soup.select_one(".job-description")
        if node is None:
            return None
        text = strip_html(node.decode_contents()).strip()
        return text or None
    except Exception:  # noqa: BLE001 - scraped markup; drift must not crash a run
        return None


class OnlineJobs:
    name = "onlinejobs"
    # We set location_restrictions ourselves rather than reading a field, and
    # the assertion is true by construction: this board only hires Filipinos.
    regions_authoritative = True

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})
        self.pages_fetched = 0
        self.hit_page_cap = False

    def _get(self, offset: int) -> str:
        url = config.ONLINEJOBS_SEARCH if offset == 0 else f"{config.ONLINEJOBS_SEARCH}/{offset}"
        return self._get_url(url)

    def _get_url(self, url: str) -> str:
        last_error = None
        for attempt in range(config.MAX_RETRIES):
            try:
                resp = self.session.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
                if resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                    time.sleep(min(wait, 30))
                    continue
                resp.raise_for_status()
                return resp.text
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                time.sleep(2 ** attempt * 0.5)
        raise RuntimeError(f"onlinejobs: giving up after {config.MAX_RETRIES} attempts: {last_error}")

    def hydrate(self, job: Job) -> Job:
        """Swap the card teaser for the full advert. One extra request.

        Called only for jobs that already survived the deterministic funnel, so
        the request count is the handful worth scoring rather than the whole
        board -- at a 5s crawl delay, hydrating everything fetched would cost
        20+ minutes to mostly re-read listings the salary floor rejects.

        Returns the job unchanged on any failure. A thinner description scores
        worse; a raised exception loses the run.
        """
        if job.source != self.name:
            return job
        # The crawl delay applies to every request, not just paging. Slept
        # before the fetch rather than after, so a caller that stops early does
        # not leave a trailing pause, and back-to-back hydrate() calls from a
        # plain loop are still spaced.
        time.sleep(config.ONLINEJOBS_CRAWL_DELAY_SECONDS)
        try:
            full = parse_detail(self._get_url(job.url))
        except Exception:  # noqa: BLE001 - one bad page must not end the run
            return job
        if not full:
            return job
        stated = job.raw.get("salary_text") if isinstance(job.raw, dict) else None
        body = f"Stated pay: {stated}\n\n{full}" if stated else full
        return replace(job, description=body, excerpt=body[:600])

    def fetch(self, since: datetime) -> Iterator[Job]:
        """Yield jobs posted at or after `since`, newest first.

        Offset pagination: /jobsearch, /jobsearch/30, /jobsearch/60. Results are
        ordered newest-first (verified 2026-08-30), so the first page with
        nothing left above the watermark ends the walk.
        """
        self.pages_fetched = 0
        self.hit_page_cap = False
        offset = 0

        while self.pages_fetched < config.ONLINEJOBS_MAX_PAGES:
            jobs = parse_page(self._get(offset))
            self.pages_fetched += 1
            if not jobs:
                return

            reached_watermark = True
            for job in jobs:
                if job.posted >= since:
                    reached_watermark = False
                    yield job

            if reached_watermark:
                return
            offset += config.ONLINEJOBS_PAGE_SIZE
            time.sleep(config.ONLINEJOBS_CRAWL_DELAY_SECONDS)

        # Fell out of the loop rather than returning: the walk was cut short,
        # and the caller must say so -- a silent cap reads as full coverage.
        self.hit_page_cap = True
