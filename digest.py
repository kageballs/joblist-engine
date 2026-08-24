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

import config

VERDICT_ORDER = {"strong": 0, "maybe": 1, "no": 2, "unscored": 3}


def _score_key(pair):
    verdict, result = pair[1].get("verdict", "unscored"), pair[1]
    return (VERDICT_ORDER.get(verdict, 9), -(result.get("score") or 0))


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
    shown = [
        (v, r) for v, r in ranked
        if r.get("score") is None or r["score"] >= profile.display_threshold
    ]

    if shown:
        # Split by the inbound line, not by score. Both groups are worth
        # applying to; they differ in whether the rate is worth pushing on.
        ask = [(v, r) for v, r in shown if v.clears_inbound_floor]
        now = [(v, r) for v, r in shown if not v.clears_inbound_floor]

        if now:
            lines.append(f"## Apply now — under ${profile.inbound_floor_hourly_usd:.0f}/hr")
            lines.append("")
            for verdict, result in now:
                lines.append(_entry(verdict, result))
        if ask:
            lines.append(
                f"## Worth the ask — ${profile.inbound_floor_hourly_usd:.0f}/hr+ or unstated"
            )
            lines.append("")
            for verdict, result in ask:
                lines.append(_entry(verdict, result))
    elif ranked:
        lines.append(f"## No matches above {profile.display_threshold}")
        lines.append("")
        lines.append(f"{len(ranked)} job(s) were scored but none cleared the threshold:")
        lines.append("")
        for verdict, result in ranked[:5]:
            lines.append(
                "- **{}** ({}) — {} — {}".format(
                    result.get("score", "?"),
                    verdict.job.company or "unknown",
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


def _entry(verdict, result) -> str:
    job = verdict.job
    score = result.get("score")
    head = "### {} — {}".format(
        "unscored" if score is None else score, job.title
    )
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
    if verdict.flagged_employer:
        bits.append(
            f"⚠️ {verdict.flagged_employer} — outsourcing intermediary, poor long-term anchor."
        )
    if verdict.local_anchor:
        bits.append("Philippines-eligible.")
    annual = job.annual_usd_max()
    if annual:
        bits.append(f"Salary: up to {annual:,} USD/yr")
    bits.append("")
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
