"""Renders a run as markdown.

The funnel line is not decoration. With a real rate floor and a hard region
filter, most days legitimately return nothing, and an empty list with no
explanation reads as a broken tool rather than a quiet market. Showing what
each stage removed -- plus the jobs that died last -- is usually more useful
than the matches themselves.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import blockers
import config
import cover

VERDICT_ORDER = {"strong": 0, "maybe": 1, "no": 2, "unscored": 3}


def _score_key(pair):
    verdict, result = pair[1].get("verdict", "unscored"), pair[1]
    return (VERDICT_ORDER.get(verdict, 9), -(result.get("score") or 0))


def _partition(ranked, profile, now):
    """Split scored pairs into kept-by-board, stale, and below-threshold.

    Both cuts are per board and both happen HERE, at render time, never at
    fetch or write time. Nothing is deleted for being stale or low, so raising
    or lowering either number and re-rendering costs nothing and rewrites no
    history. Same discipline as report.py --reapply.
    """
    kept: dict[str, list] = {}
    stale, below = [], []
    for verdict, result in ranked:
        board = profile.board(verdict.job.source)
        if board.is_stale(verdict.job.posted, now):
            stale.append((verdict, result))
            continue
        score = result.get("score")
        if score is not None and score < board.display_threshold:
            below.append((verdict, result))
            continue
        kept.setdefault(verdict.job.source, []).append((verdict, result))
    return kept, stale, below


def render(funnel, scored_pairs, profile, model, started_at, no_llm=False) -> str:
    """`scored_pairs` is a list of (Verdict, result dict)."""
    lines = []
    stamp = started_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"# Job digest — {stamp}")
    lines.append("")
    lines.append(f"`{funnel.line()}`")
    lines.append("")

    if no_llm:
        lines.append("_Deterministic filters only (`--no-llm`); nothing was scored._")
        lines.append("")

    ranked = sorted(scored_pairs, key=_score_key)
    kept, stale, below = _partition(ranked, profile, started_at)

    if stale:
        lines.append(
            f"_{len(stale)} listing(s) hidden as stale by their board's own age window._"
        )
        lines.append("")

    if kept:
        # Sectioned by board, never pooled. A score is only comparable within
        # the board that produced it: on the author's own history the same
        # rubric and model average 20.3 on one board and 5.9 on another, so a
        # single ranked list across boards ranks nothing.
        for board_name in sorted(kept):
            rows = kept[board_name]
            board = profile.board(board_name)
            lines.append(f"## {board_name} — {len(rows)} listing(s)")
            if board.display_threshold:
                lines.append("")
                lines.append(f"_Showing score {board.display_threshold}+ for this board._")
            lines.append("")

            # Within a board, split by the inbound line rather than by score.
            # Both groups are worth applying to; they differ in whether the
            # rate is worth pushing back on.
            ask = [(v, r) for v, r in rows if v.clears_inbound_floor]
            now = [(v, r) for v, r in rows if not v.clears_inbound_floor]
            if now:
                lines.append(f"### Apply now — under ${profile.inbound_floor_hourly_usd:.0f}/hr")
                lines.append("")
                for verdict, result in now:
                    lines.append(_entry(verdict, result, profile))
            if ask:
                lines.append(
                    f"### Worth the ask — ${profile.inbound_floor_hourly_usd:.0f}/hr+ or unstated"
                )
                lines.append("")
                for verdict, result in ask:
                    lines.append(_entry(verdict, result, profile))
    elif below:
        lines.append("## No matches cleared their board's threshold")
        lines.append("")
        lines.append(f"{len(below)} job(s) were scored but none cleared it:")
        lines.append("")
        for verdict, result in below[:5]:
            lines.append(
                "- **{}** ({}) [{}] — {} — {}".format(
                    result.get("score", "?"),
                    verdict.job.company or "unknown",
                    verdict.job.source,
                    verdict.job.title,
                    result.get("why", ""),
                )
            )
        lines.append("")
    else:
        lines.append("## Nothing survived the filters")
        lines.append("")
        lines.append("This is a normal outcome, not a failure. The near misses below")
        lines.append("show where the funnel is cutting.")
        lines.append("")

    near = funnel.near_misses(5)
    if near:
        lines.append("## Near misses")
        lines.append("")
        lines.append("Rejected latest in the chain — the most informative cuts.")
        lines.append("")
        for verdict in near:
            lines.append(
                f"- `{verdict.rejected_by}` **{verdict.job.title}** — {verdict.reason}"
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("Model: `{}` · profile: `{}`".format(model if not no_llm else "none", profile.name))
    lines.append("")
    return "\n".join(lines)


def _entry(verdict, result, profile) -> str:
    job = verdict.job
    score = result.get("score")
    raw = result.get("score_raw")
    # Show the arithmetic when a penalty moved the score. A silently docked
    # number looks like the model rating the work poorly, which is the opposite
    # of what happened -- the work may be a fine match you simply cannot apply to.
    shown = "unscored" if score is None else str(score)
    if score is not None and raw is not None and raw != score:
        shown = f"{score} (was {raw})"
    head = f"### {shown} — {job.title}"
    bits = [head, ""]
    bits.append("**{}** · {} · posted {:%Y-%m-%d}".format(
        job.company or "unknown company",
        ", ".join(job.location_restrictions) or "worldwide",
        job.posted,
    ))
    bits.append("")
    if result.get("why"):
        bits.append(result["why"])
        bits.append("")
    if result.get("matched_skills"):
        bits.append("Matches: " + ", ".join(result["matched_skills"][:8]))
    if result.get("concerns"):
        bits.append("Concerns: " + "; ".join(result["concerns"][:4]))
    if result.get("blockers"):
        bits.append(
            "🚫 Requires what you cannot supply: "
            + blockers.describe(result["blockers"])
            + "."
        )
    if result.get("soft_blockers"):
        # Asked for but not a condition: worth answering in the cover note
        # rather than a reason to skip the listing.
        bits.append(
            "Asks for, but does not require: "
            + blockers.describe(result["soft_blockers"])
            + "."
        )
    if verdict.flagged_employer:
        bits.append(
            f"⚠️ {verdict.flagged_employer} — outsourcing intermediary, poor long-term anchor."
        )
    if result.get("manual_steps"):
        # Above the salary and anchor lines on purpose: this is the thing that
        # decides whether the job can be applied to today or needs an evening
        # first. It never affected the score and must never read as if it did.
        bits.append(
            "BEFORE APPLYING: " + blockers.describe(result["manual_steps"]) + "."
        )
    if verdict.local_anchor:
        bits.append(
            ", ".join(profile.local_anchor_regions) + "-eligible."
        )
    annual = job.annual_usd_max()
    if annual:
        bits.append(f"Salary: up to {annual:,} USD/yr")
    bits.append("")
    draft = cover.out_path(job.key)
    if draft.exists():
        bits.append(f"Draft letter: `{draft}`")
    bits.append("CV to send: **{}** · [apply]({})".format(
        result.get("cv_variant", "engineering"), job.url
    ))
    bits.append("")
    return "\n".join(bits)


def write(text: str, when: datetime | None = None) -> str:
    """Append to the day's digest rather than replacing it.

    Runs are per-day files but there can be several runs a day, and the second
    one usually finds nothing new. Overwriting would silently delete the
    morning's matches, which is the one outcome worse than finding nothing.
    """
    when = when or datetime.now(UTC)
    os.makedirs(config.DIGEST_DIR, exist_ok=True)
    path = os.path.join(config.DIGEST_DIR, when.strftime("%Y-%m-%d") + ".md")
    mode = "a" if os.path.exists(path) else "w"
    with open(path, mode, encoding="utf-8") as fh:
        if mode == "a":
            fh.write("\n\n")
        fh.write(text)
    return path
