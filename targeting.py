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
    inbound_floor_hourly_usd: float
    role_include: tuple[re.Pattern, ...]
    role_exclude: tuple[re.Pattern, ...]
    role_exclude_unless_paid: tuple[re.Pattern, ...]
    employer_blocklist: tuple[str, ...]
    employer_flags: tuple[str, ...]
    resume_path: str
    display_threshold: int
    # Requirement keys (config.REQUIREMENT_KINDS) this person cannot supply.
    # A frozenset so membership is cheap and the value cannot be mutated by a
    # caller holding the profile.
    cannot_provide: frozenset[str]
    blocker_penalty: int
    resume: str = field(default="", repr=False)

    def flagged_employer(self, company: str) -> str | None:
        """Known rate-anchoring employer: surfaced with a reason, not hidden.

        Distinct from `employer_blocklist`, which rejects outright. A flag
        keeps the listing visible and lets the scorer explain the tradeoff,
        so the choice is made per posting rather than by never seeing it.
        """
        text = (company or "").casefold()
        if not text:
            return None
        for name in self.employer_flags:
            if name.casefold() in text:
                return name
        return None

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

    # The line between "apply now" and "worth pushing on rate". Outbound
    # applications take the lower floor; this is the number you would hold if
    # they had approached you, and it only ever affects presentation.
    inbound_floor = float(rate.get("inbound_floor_hourly_usd", target))

    deliverables = raw.get("deliverables") or {}
    cannot = [str(k).strip() for k in (deliverables.get("cannot_provide") or []) if str(k).strip()]
    # Fail loudly on a typo. A key that matches nothing would otherwise sit in
    # the profile looking effective while silently flagging zero listings --
    # exactly the failure this feature exists to prevent.
    unknown = sorted(set(cannot) - set(config.REQUIREMENT_KINDS))
    if unknown:
        raise ProfileError(
            "deliverables.cannot_provide has "
            f"{'keys' if len(unknown) > 1 else 'a key'} that is not a requirement kind: "
            + ", ".join(unknown)
            + "\nValid keys: "
            + ", ".join(sorted(config.REQUIREMENT_KINDS))
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
        inbound_floor_hourly_usd=inbound_floor,
        role_include=_patterns(roles.get("include"), "include"),
        role_exclude=_patterns(roles.get("exclude"), "exclude"),
        role_exclude_unless_paid=_patterns(
            roles.get("exclude_unless_paid"), "exclude_unless_paid"
        ),
        employer_blocklist=tuple(raw.get("employers", {}).get("blocklist") or []),
        employer_flags=tuple(raw.get("employers", {}).get("flag") or []),
        resume_path=resume_path,
        display_threshold=int(scoring.get("display_threshold", 60)),
        cannot_provide=frozenset(cannot),
        blocker_penalty=int(deliverables.get("blocker_penalty", 30)),
        resume=resume,
    )
