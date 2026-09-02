"""The deterministic funnel.

Ordered cheapest-and-most-eliminating first, so the expensive stages only ever
see what survived the free ones. On a measured 300-job sample of Himalayas,
region plus timezone alone removed 97.7% -- which is the whole point: the LLM
is not a filter, it is a ranker for the handful that get through.

Every stage returns None to pass, or a short reason to reject. Rejections are
recorded locally so that widening a rule later can replay what it threw away.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sources import base
from sources.base import Job
from targeting import Profile

# Eligibility restrictions hide at the BOTTOM of a job ad, in the boilerplate
# under the benefits. Any truncation strategy that keeps the head loses them,
# so these sentences are extracted whole and carried to the scorer verbatim.
_ELIGIBILITY = re.compile(
    r"[^.!?\n]*\b("
    r"must (?:be )?(?:located|reside|live|based)"
    r"|only (?:able to |consider)?(?:hire|employ|accept)"
    r"|work authorization|authorized to work|right to work"
    r"|visa sponsorship|cannot sponsor|no sponsorship"
    r"|residing in|residents of|eligible to work"
    r")\b[^.!?\n]*[.!?]?",
    re.I,
)

SALARY_ABOVE = "above"
SALARY_BELOW = "below"
SALARY_UNKNOWN = "unknown"

HOURS_PER_YEAR = 2080


def eligibility_sentences(text: str, limit: int = 4) -> list[str]:
    """Pull the sentences that state who may actually be hired."""
    seen = set()
    out = []
    for match in _ELIGIBILITY.finditer(text or ""):
        sentence = " ".join(match.group(0).split())
        key = sentence.casefold()
        if len(sentence) > 12 and key not in seen:
            seen.add(key)
            out.append(sentence)
        if len(out) >= limit:
            break
    return out


@dataclass
class Verdict:
    job: Job
    rejected_by: str | None = None
    reason: str = ""
    salary_signal: str = SALARY_UNKNOWN
    local_anchor: bool = False
    flagged_employer: str | None = None
    eligibility_notes: list[str] = field(default_factory=list)
    # True when this board's region field cannot be trusted AND the listing
    # states no restriction. The funnel still passes it -- rejecting on absent
    # data would break the same tri-state discipline that governs salary --
    # but the scorer is told not to read the silence as "worldwide".
    region_unverified: bool = False
    # Splits the digest, never rejects. True when the stated rate clears the
    # inbound line, and also when nothing is stated at all — an unstated rate
    # is a rate worth asking about.
    clears_inbound_floor: bool = True

    @property
    def passed(self) -> bool:
        return self.rejected_by is None


def stage_expired(job: Job, profile: Profile, now: datetime) -> str | None:
    if job.expires and job.expires < now:
        return f"expired {job.expires:%Y-%m-%d}"
    return None


def stage_region(job: Job, profile: Profile, now: datetime) -> str | None:
    """The highest-value filter, and free.

    An empty restriction list means genuinely worldwide -- that is the good
    case and it passes. A non-empty list must admit this person explicitly.

    An empty list only means that on a board whose region field is
    authoritative. Where it is not, the restriction is routinely buried in the
    prose instead ("we can only hire employees residing in British Columbia or
    Ontario" under a `region` of Anywhere in the World), so an empty tuple
    proves nothing and the listing has to reach the scorer to be read properly.
    """
    if not job.location_restrictions:
        return None
    if any(profile.region_allowed(r) for r in job.location_restrictions):
        return None
    return "hiring limited to " + ", ".join(job.location_restrictions[:3])


def stage_timezone(job: Job, profile: Profile, now: datetime) -> str | None:
    if not job.timezone_restrictions:
        return None
    if set(job.timezone_restrictions) & profile.accept_offsets:
        return None
    offsets = ", ".join(f"UTC{o:+d}" for o in job.timezone_restrictions[:4])
    return "requires " + offsets


def stage_employer(job: Job, profile: Profile, now: datetime) -> str | None:
    company = (job.company or "").casefold()
    if not company:
        return None
    for blocked in profile.employer_blocklist:
        if blocked.casefold() in company:
            return f"blocklisted employer ({job.company})"
    return None


def stage_salary(job: Job, profile: Profile, now: datetime) -> str | None:
    """Tri-state, and it must stay that way.

    Most listings state no salary at all. Treating unknown as a rejection is
    the single most likely way this tool ships and then returns nothing
    forever, so only a figure that is CERTAINLY below the absolute floor is
    rejected -- and against the floor, never the target ask.
    """
    annual = job.annual_usd_max()
    if annual is None:
        return None
    # The board's own floor, not a global one. A domestic board pays local
    # rates, and judging it against the international number rejects the whole
    # board; judging the international board against the local number lets
    # everything through. Neither is a compromise worth making.
    floor_hourly = profile.board(job.source).absolute_floor_hourly_usd
    floor_annual = floor_hourly * HOURS_PER_YEAR
    if annual < floor_annual:
        return f"pays up to {annual:,.0f} USD/yr, floor is {floor_annual:,.0f}"
    return None


def stage_role(job: Job, profile: Profile, now: datetime) -> str | None:
    """Deliberately last of the rejecting stages, and deliberately loose.

    Regex is worst at exactly what a human reads a title for, so this only
    removes the obviously unrelated -- the accounting coordinators that
    survive a region filter -- rather than trying to judge fit.
    """
    haystack = " ".join([job.title, " ".join(job.categories)])
    for pattern in profile.role_exclude:
        if pattern.search(haystack):
            return f"excluded title ({pattern.pattern})"

    # Titles worth taking only when the money is confirmed — junior bands and
    # the like. This leans on stage order: `salary` runs before `role`, so
    # anything with a stated rate BELOW the floor is already gone by now. The
    # only case left to catch is a stated rate that is missing entirely.
    #
    # A non-USD figure also counts as unconfirmed, because annual_usd_max()
    # will not guess a conversion. That over-rejects slightly, and it is the
    # right way round: the point of this rule is confirmation.
    if profile.role_exclude_unless_paid and job.annual_usd_max() is None:
        for pattern in profile.role_exclude_unless_paid:
            match = pattern.search(haystack)
            if match:
                return f"'{match.group(0)}' title and no salary stated"
    if not profile.role_include:
        return None
    if any(pattern.search(haystack) for pattern in profile.role_include):
        return None
    return "title matches no target role"


STAGES = [
    ("expired", stage_expired),
    ("region", stage_region),
    ("timezone", stage_timezone),
    ("employer", stage_employer),
    ("salary", stage_salary),
    ("role", stage_role),
]


def evaluate(job: Job, profile: Profile, now: datetime | None = None,
             source=None) -> Verdict:
    """`source` is the Source instance the job came from, when the caller has it.

    Optional so that a test can evaluate a bare Job, in which case the board is
    assumed to publish trustworthy fields -- the same default the Source
    protocol declares.
    """
    now = now or datetime.now(UTC)
    verdict = Verdict(job=job)

    if source is not None and not job.location_restrictions:
        verdict.region_unverified = not base.capability(source, "regions_authoritative")

    annual = job.annual_usd_max()
    if annual is not None:
        target_annual = profile.target_hourly_usd * HOURS_PER_YEAR
        verdict.salary_signal = SALARY_ABOVE if annual >= target_annual else SALARY_BELOW
        # An unstated rate stays True: nothing to push back on yet.
        verdict.clears_inbound_floor = annual >= profile.inbound_floor_hourly_usd * HOURS_PER_YEAR

    verdict.local_anchor = profile.is_local_anchor(job.location_restrictions)
    verdict.flagged_employer = profile.flagged_employer(job.company)
    verdict.eligibility_notes = eligibility_sentences(job.description)

    for name, stage in STAGES:
        reason = stage(job, profile, now)
        if reason:
            verdict.rejected_by = name
            verdict.reason = reason
            break
    return verdict


class Funnel:
    """Counts what each stage removed, so a zero-result day is explicable.

    Zero-result days are the expected case with a real rate floor. An empty
    list with no explanation reads as broken, and the tool stops being opened.
    """

    def __init__(self):
        self.fetched = 0
        self.seen_skipped = 0
        self.by_stage: dict[str, int] = {}
        self.survivors: list[Verdict] = []
        self.rejects: list[Verdict] = []

    def add(self, verdict: Verdict) -> None:
        if verdict.passed:
            self.survivors.append(verdict)
        else:
            key = verdict.rejected_by
            self.by_stage[key] = self.by_stage.get(key, 0) + 1
            self.rejects.append(verdict)

    def near_misses(self, n: int = 3) -> list[Verdict]:
        """The ones that died latest in the chain -- the most informative rejects."""
        order = {name: i for i, (name, _) in enumerate(STAGES)}
        return sorted(self.rejects, key=lambda v: -order.get(v.rejected_by, 0))[:n]

    def line(self) -> str:
        parts = [f"{self.fetched} fetched"]
        if self.seen_skipped:
            parts.append(f"{self.seen_skipped} already seen")
        for name, _ in STAGES:
            removed = self.by_stage.get(name, 0)
            if removed:
                parts.append(f"-{removed} {name}")
        parts.append(f"{len(self.survivors)} to score")
        return "  ".join(parts)
