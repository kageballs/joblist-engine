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
class BoardPolicy:
    """What this operator asks of ONE board.

    Every number here is per board on purpose. A score, a rate floor and an age
    limit are only meaningful within the board they came from: region and
    timezone eliminate ~97.7% of Himalayas for zero tokens and reject nothing
    on OnlineJobs, where salary is the only filter doing work, and OnlineJobs
    tops out below the rate floor that Himalayas clears comfortably. There is
    no single value that is correct for both, so there is no global one.
    """

    name: str
    display_threshold: int
    draft_at: int
    absolute_floor_hourly_usd: float
    # None means "never trim by age on this board".
    max_age_days: int | None = None
    # The lower bar that applies only when the advert itself asks for a cover
    # letter. Two different questions: `draft_at` asks "is this good enough to
    # be worth a letter", `draft_if_asked_at` asks "the employer has said the
    # application is incomplete without one -- is this still worth applying to
    # at all".
    #
    # None resolves to `draft_at` below, which is what makes this additive: a
    # board that has not opted in behaves exactly as it did before, and every
    # BoardPolicy built without the field keeps working.
    draft_if_asked_at: int | None = None

    def __post_init__(self):
        if self.draft_if_asked_at is None:
            object.__setattr__(self, "draft_if_asked_at", self.draft_at)

    def is_stale(self, posted, now) -> bool:
        """Age check, applied when rendering rather than when fetching.

        Nothing is deleted for being stale, so raising or lowering the window
        re-renders differently with no refetch and no rewritten history. Same
        discipline as report.py --reapply.
        """
        if self.max_age_days is None or posted is None:
            return False
        return (now - posted).days > self.max_age_days


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
    # Requirement keys this person CAN supply, but only by doing something
    # first: recording a video, sitting a test task. Deliberately separate from
    # cannot_provide, and deliberately never priced -- see blockers.evaluate.
    needs_manual_step: frozenset[str] = frozenset()
    boards: dict[str, BoardPolicy] = field(default_factory=dict, repr=False)
    resume: str = field(default="", repr=False)

    def board(self, name: str) -> BoardPolicy:
        """Policy for one board. Raises rather than inventing a default.

        A board with no block in profile.yaml is a configuration gap, not a
        board to be treated like its neighbour. Falling back to another board's
        numbers is the exact mistake this whole structure exists to prevent, so
        it fails loudly instead.
        """
        try:
            return self.boards[name]
        except KeyError:
            known = ", ".join(sorted(self.boards)) or "none"
            raise ProfileError(
                f"no policy for board {name!r} in profile.yaml.\n"
                f"Add a `boards.{name}:` block. Configured boards: {known}.\n"
                "Boards are not interchangeable: a threshold, floor or age "
                "window from one board is meaningless on another, so this is "
                "not defaulted for you."
            ) from None

    def require_boards(self, names) -> None:
        """Fail before a run starts if any active source has no policy."""
        missing = [n for n in names if n not in self.boards]
        if missing:
            self.board(missing[0])  # raises with the actionable message

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

    manual = [
        str(k).strip()
        for k in (deliverables.get("needs_manual_step") or [])
        if str(k).strip()
    ]
    unknown = sorted(set(manual) - set(config.REQUIREMENT_KINDS))
    if unknown:
        raise ProfileError(
            "deliverables.needs_manual_step has "
            f"{'keys' if len(unknown) > 1 else 'a key'} that is not a requirement kind: "
            + ", ".join(unknown)
            + "\nValid keys: "
            + ", ".join(sorted(config.REQUIREMENT_KINDS))
        )
    overlap = sorted(set(manual) & set(cannot))
    if overlap:
        raise ProfileError(
            "these keys are in both deliverables.cannot_provide and "
            "deliverables.needs_manual_step: " + ", ".join(overlap) + "\n"
            "They mean opposite things. cannot_provide is what you can never "
            "supply, and it docks the score. needs_manual_step is what you can "
            "supply after doing some work, and it never touches the score. "
            "Pick one per key."
        )

    boards = _boards(raw, default_threshold=int(scoring.get("display_threshold", 60)),
                     default_floor=floor)

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
        needs_manual_step=frozenset(manual),
        boards=boards,
        resume=resume,
    )


def _boards(raw: dict, default_threshold: int, default_floor: float) -> dict[str, BoardPolicy]:
    """Parse the `boards:` map.

    `boards.defaults` fills in keys a board omits, and the top-level `scoring`
    and `rate` values fill in what `defaults` itself omits, so a profile
    written before this existed still loads. What is deliberately NOT provided
    is a fallback for a board with no block at all: see Profile.board.
    """
    section = raw.get("boards") or {}
    if not isinstance(section, dict):
        raise ProfileError("`boards:` must be a mapping of board name to its settings.")

    base = section.get("defaults") or {}
    if not isinstance(base, dict):
        raise ProfileError("`boards.defaults:` must be a mapping.")

    def _num(source, key, fallback):
        value = source.get(key, base.get(key, fallback))
        return None if value is None else value

    out: dict[str, BoardPolicy] = {}
    for name, cfg in section.items():
        if name == "defaults":
            continue
        cfg = cfg or {}
        if not isinstance(cfg, dict):
            raise ProfileError(f"`boards.{name}:` must be a mapping, or empty to take defaults.")
        threshold = int(_num(cfg, "display_threshold", default_threshold))
        age = _num(cfg, "max_age_days", None)
        # A board that never says when to draft still should not draft
        # everything it displays, so this tracks the threshold rather than
        # defaulting to zero.
        draft_at = int(_num(cfg, "draft_at", threshold))
        out[name] = BoardPolicy(
            name=name,
            display_threshold=threshold,
            draft_at=draft_at,
            # Defaults to draft_at rather than to something lower: a board that
            # has not been told to relax its bar must not start drafting for
            # adverts it would otherwise have skipped. Opting in is the
            # operator's decision, per board, like everything else here.
            draft_if_asked_at=int(_num(cfg, "draft_if_asked_at", draft_at)),
            absolute_floor_hourly_usd=float(_num(cfg, "absolute_floor_hourly_usd", default_floor)),
            max_age_days=None if age is None else int(age),
        )
    return out
