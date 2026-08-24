"""Himalayas job feed.

The only source that publishes structured eligibility data — salary, hiring
regions, and required timezone offsets as real fields rather than prose. That
is what lets the deterministic filter eliminate ~96% of listings before a
single token is spent, so this source carries the whole v1.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime

import requests

import config
from sources.base import Job, strip_html


def _epoch(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _offsets(values) -> tuple[int, ...]:
    out = []
    for v in values or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return tuple(out)


def to_job(item: dict) -> Job | None:
    """Map one API item onto Job. Returns None for anything unusable.

    Never raises on a missing key: the feed is undocumented as a contract and
    Himalayas deprecated `offset` on 2026-08-21 with no notice, so field drift
    is expected rather than exceptional.
    """
    guid = item.get("guid") or item.get("applicationLink")
    title = (item.get("title") or "").strip()
    posted = _epoch(item.get("pubDate"))
    if not guid or not title or posted is None:
        return None

    return Job(
        source="himalayas",
        source_id=str(guid),
        title=title,
        company=(item.get("companyName") or "").strip(),
        url=item.get("applicationLink") or str(guid),
        posted=posted,
        excerpt=strip_html(item.get("excerpt"))[:600],
        description=strip_html(item.get("description")),
        location_restrictions=tuple(
            str(x).strip() for x in (item.get("locationRestrictions") or []) if str(x).strip()
        ),
        timezone_restrictions=_offsets(item.get("timezoneRestrictions")),
        salary_min=item.get("minSalary") or None,
        salary_max=item.get("maxSalary") or None,
        salary_period=(item.get("salaryPeriod") or None),
        currency=(item.get("currency") or None),
        seniority=tuple(str(x) for x in (item.get("seniority") or [])),
        categories=tuple(str(x) for x in (item.get("categories") or [])),
        expires=_epoch(item.get("expiryDate")),
        raw=item,
    )


class Himalayas:
    name = "himalayas"
    # Region and timezone fields here are trustworthy enough to reject on.
    # Sources where they are not (WWR says "Anywhere in the World" on jobs
    # restricted to two Canadian provinces) must set this False.
    regions_authoritative = True

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})
        self.pages_fetched = 0
        self.hit_page_cap = False

    def _get(self, params: dict) -> dict:
        last_error = None
        for attempt in range(config.MAX_RETRIES):
            try:
                resp = self.session.get(
                    config.HIMALAYAS_API,
                    params=params,
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                )
                if resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                    time.sleep(min(wait, 30))
                    continue
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                time.sleep(2 ** attempt * 0.5)
        raise RuntimeError(f"himalayas: giving up after {config.MAX_RETRIES} attempts: {last_error}")

    def fetch(self, since: datetime) -> Iterator[Job]:
        """Yield jobs posted at or after `since`, newest first.

        The feed is ordered newest-first, so the first page whose entire
        contents predate the watermark ends the walk. Cursor pagination only:
        `offset` is deprecated upstream.
        """
        cursor = None
        self.pages_fetched = 0
        self.hit_page_cap = False

        while self.pages_fetched < config.HIMALAYAS_MAX_PAGES:
            params = {"limit": config.HIMALAYAS_PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor

            payload = self._get(params)
            items = payload.get("jobs") or []
            self.pages_fetched += 1
            if not items:
                return

            reached_watermark = True
            for item in items:
                job = to_job(item)
                if job is None:
                    continue
                if job.posted >= since:
                    reached_watermark = False
                    yield job

            cursor = payload.get("nextCursor")
            if not cursor or reached_watermark:
                return
            time.sleep(config.REQUEST_DELAY_SECONDS)

        # Fell out of the loop rather than returning: the walk was cut short.
        # The caller must say so — a silent cap reads as full coverage.
        self.hit_page_cap = True
