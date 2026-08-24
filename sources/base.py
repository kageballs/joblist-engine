"""Normalised job model and the Source protocol.

Every source returns `Job` objects. Anything source-specific — pagination,
HTML stripping, field naming — stays inside that source's module.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&apos;": "'", "&nbsp;": " ",
}


def strip_html(raw: str | None) -> str:
    """Flatten an HTML fragment to readable plain text.

    Deliberately not a parser: job descriptions are display HTML, not documents,
    and every source hands us a different dialect of it.
    """
    if not raw:
        return ""
    text = re.sub(r"<(br|/p|/li|/h[1-6]|/div)[^>]*>", "\n", raw, flags=re.I)
    text = _TAG.sub(" ", text)
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = _WS.sub(" ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    return text.strip()


@dataclass(frozen=True)
class Job:
    """One listing, normalised across sources.

    `location_restrictions` is the highest-signal field in the whole model:
    an empty tuple means genuinely worldwide, which is what we are hunting for.
    A non-empty tuple is a hard gate — see filter.stage_location.
    """

    source: str
    source_id: str
    title: str
    company: str
    url: str
    posted: datetime
    excerpt: str = ""
    description: str = ""
    location_restrictions: tuple[str, ...] = ()
    timezone_restrictions: tuple[int, ...] = ()
    salary_min: int | None = None
    salary_max: int | None = None
    salary_period: str | None = None  # annual | monthly | hourly
    currency: str | None = None
    seniority: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    expires: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def key(self) -> str:
        """Dedupe key, namespaced so sources with clashing id schemes cannot collide."""
        return f"{self.source}:{self.source_id}"

    def annual_usd_max(self) -> int | None:
        """Best-effort ceiling of the stated range, normalised to USD/year.

        Returns None when the listing states no salary (the common case) or
        quotes a currency we are not confident converting — callers must treat
        None as "unknown", never as "zero".
        """
        if not self.salary_max:
            return None
        if self.currency and self.currency.upper() != "USD":
            return None
        multiplier = {"annual": 1, "yearly": 1, "monthly": 12, "hourly": 2080}
        factor = multiplier.get((self.salary_period or "annual").lower())
        if factor is None:
            return None
        return int(self.salary_max * factor)


class Source(Protocol):
    """A job feed. One method, so adding a source stays cheap."""

    name: str

    def fetch(self, since: datetime) -> Iterator[Job]:
        """Yield jobs posted at or after `since`, newest first."""
        ...
