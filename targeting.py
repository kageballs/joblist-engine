"""Loads profile.yaml — the targeting rules, kept out of the repo.

Everything personal lives here rather than in code: the rate floor, the
employer blocklist, the resume. The engine is generic; the profile is yours.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import yaml

import config


class ProfileError(RuntimeError):
    """Raised with an actionable message — this is the first thing a new user hits."""


@dataclass(frozen=True)
class Profile:
    name: str
    based_in: str
    utc_offset: int
    allow_regions: tuple[str, ...]
    deny_regions: tuple[str, ...]
    local_anchor_regions: tuple[str, ...]
    accept_offsets: frozenset[int]
    target_hourly_usd: float
    absolute_floor_hourly_usd: float
    role_include: tuple[re.Pattern, ...]
    role_exclude: tuple[re.Pattern, ...]
    employer_blocklist: tuple[str, ...]
    resume_path: str
    display_threshold: int
    resume: str = field(default="", repr=False)

    def region_allowed(self, restriction: str) -> bool:
        """Does one stated restriction admit this person?

        Substring matching in both directions: feeds write "United States",
        "Anywhere in the World", and "APAC" with no shared vocabulary.
        """
        text = restriction.strip().casefold()
        if not text:
            return True
        if any(d.casefold() in text for d in self.deny_regions):
            return False
        return any(a.casefold() in text or text in a.casefold() for a in self.allow_regions)

    def is_local_anchor(self, restrictions: tuple[str, ...]) -> bool:
        """Restricted to the user's own country — usually local-rate employers."""
        return bool(restrictions) and all(
            any(a.casefold() in r.casefold() for a in self.local_anchor_regions)
            for r in restrictions
        )


def _patterns(values, field_name: str) -> tuple[re.Pattern, ...]:
    out = []
    for v in values or []:
        try:
            out.append(re.compile(str(v), re.I))
        except re.error as exc:
            raise ProfileError(f"roles.{field_name}: {v!r} is not a valid regex ({exc})") from exc
    return tuple(out)


def load(path: str | None = None) -> Profile:
    path = path or config.PROFILE_PATH
    if not os.path.exists(path):
        raise ProfileError(
            f"{path} not found.\n"
            f"  cp profile.example.yaml {path}\n"
            f"then edit it. It is gitignored and stays on this machine."
        )
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    for section in ("identity", "regions", "timezone", "rate", "roles", "scoring"):
        if section not in raw:
            raise ProfileError(f"{path} is missing the '{section}' section.")

    ident, regions = raw["identity"], raw["regions"]
    rate, roles, scoring = raw["rate"], raw["roles"], raw["scoring"]

    floor = float(rate["absolute_floor_hourly_usd"])
    target = float(rate["target_hourly_usd"])
    if floor > target:
        raise ProfileError(
            f"rate.absolute_floor_hourly_usd ({floor}) is above "
            f"rate.target_hourly_usd ({target}) — the floor is the reject line, "
            f"the target is what you ask for."
        )

    resume_path = scoring.get("resume_path", "data/resume.md")
    resume = ""
    if os.path.exists(resume_path):
        with open(resume_path, encoding="utf-8") as fh:
            resume = fh.read().strip()

    return Profile(
        name=str(ident.get("name", "")),
        based_in=str(ident.get("based_in", "")),
        utc_offset=int(ident["utc_offset"]),
        allow_regions=tuple(regions.get("allow") or []),
        deny_regions=tuple(regions.get("deny") or []),
        local_anchor_regions=tuple(regions.get("local_anchor") or []),
        accept_offsets=frozenset(int(x) for x in raw["timezone"].get("accept") or []),
        target_hourly_usd=target,
        absolute_floor_hourly_usd=floor,
        role_include=_patterns(roles.get("include"), "include"),
        role_exclude=_patterns(roles.get("exclude"), "exclude"),
        employer_blocklist=tuple(raw.get("employers", {}).get("blocklist") or []),
        resume_path=resume_path,
        display_threshold=int(scoring.get("display_threshold", 60)),
        resume=resume,
    )
