#!/usr/bin/env python3
"""
JobList - Onlinejobs.ph AI/Automation Job Matcher

Usage:
    python main.py                # Full run: scrape, match, save seen IDs, print JSON
    python main.py --dry-run      # Scrape and match but do NOT update seen_jobs.json
    python main.py --init-resume  # Extract PDFs -> data/resume.md (run once to bootstrap)
    python main.py --help         # Show this help

Resume workflow:
    1. Drop Resume_2026.pdf and "Past Projects _2026.pdf" into data/
    2. Run: python main.py --init-resume
       -> Creates data/resume.md with all content extracted from the PDFs
    3. Edit data/resume.md freely to add/update skills and experience
    4. Run: python main.py --dry-run  to test matching
    5. Daily runs always read from data/resume.md (not the PDFs)

Output:
    Prints a JSON array of matched jobs to stdout.
    The scheduled Claude Code agent reads this output and emails it via Gmail MCP.
"""

import sys
import json
import os
from datetime import date

from config import (
    DATA_DIR, RESUME_MD_PATH, RESUME_PDF_PATH, PROJECTS_PDF_PATH,
    SEEN_JOBS_PATH, MATCH_THRESHOLD, RECIPIENT_EMAIL,
)


def _ensure_data_files() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(SEEN_JOBS_PATH):
        with open(SEEN_JOBS_PATH, "w") as f:
            json.dump([], f)


def _extract_pdf_text(path: str) -> str:
    try:
        import pdfplumber
    except ImportError:
        print("[main] pdfplumber not installed. Run: pip install pdfplumber", file=sys.stderr)
        return ""
    if not os.path.exists(path):
        return ""
    with pdfplumber.open(path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages).strip()


def init_resume() -> None:
    """Extract content from PDFs and write data/resume.md for ongoing editing."""
    sections: list[str] = []

    resume_text = _extract_pdf_text(RESUME_PDF_PATH)
    if resume_text:
        sections.append("## Resume\n\n" + resume_text)
        print(f"[init] Extracted resume from {RESUME_PDF_PATH}", file=sys.stderr)
    else:
        print(f"[init] WARNING: {RESUME_PDF_PATH} not found or empty", file=sys.stderr)

    projects_text = _extract_pdf_text(PROJECTS_PDF_PATH)
    if projects_text:
        sections.append("## Past Projects\n\n" + projects_text)
        print(f"[init] Extracted past projects from {PROJECTS_PDF_PATH}", file=sys.stderr)
    else:
        print(f"[init] WARNING: {PROJECTS_PDF_PATH} not found or empty", file=sys.stderr)

    if not sections:
        print("[init] ERROR: No PDF content found. Place PDFs in data/ and retry.", file=sys.stderr)
        sys.exit(1)

    header = (
        "# My Resume & Skills\n\n"
        "> Edit this file freely to add, remove, or update skills and experience.\n"
        "> This file is what the job matcher reads — the PDFs are no longer needed after init.\n\n"
    )
    content = header + "\n\n---\n\n".join(sections)

    with open(RESUME_MD_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"[init] Created {RESUME_MD_PATH} ({len(content)} chars)", file=sys.stderr)
    print(f"[init] Edit this file to update your skills, then run: python main.py --dry-run", file=sys.stderr)


def _load_resume() -> str:
    if not os.path.exists(RESUME_MD_PATH):
        print(
            f"[ERROR] {RESUME_MD_PATH} not found.\n"
            "Run first: python main.py --init-resume",
            file=sys.stderr,
        )
        return ""
    with open(RESUME_MD_PATH, "r", encoding="utf-8") as f:
        return f.read().strip()


def _print_summary(matches: list[dict]) -> None:
    today = date.today().isoformat()
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  JobBot Daily Run - {today}", file=sys.stderr)
    print(f"  High-probability matches (score >= {MATCH_THRESHOLD}): {len(matches)}", file=sys.stderr)
    print(f"  Recipient: {RECIPIENT_EMAIL}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    for i, m in enumerate(matches, 1):
        print(f"  {i}. [{m.get('score', 0):3d}] {m.get('title', 'N/A')}", file=sys.stderr)
        if m.get("salary"):
            print(f"      Salary : {m['salary']}", file=sys.stderr)
        print(f"      Link   : {m.get('url', 'N/A')}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)

    if "--init-resume" in sys.argv:
        _ensure_data_files()
        init_resume()
        sys.exit(0)

    _ensure_data_files()

    resume_text = _load_resume()
    if not resume_text:
        sys.exit(1)

    from agents import orchestrator

    print("[main] Starting job search agent...", file=sys.stderr)
    matches = orchestrator.run(resume_text, dry_run=dry_run)

    _print_summary(matches)

    if dry_run:
        print("[main] Dry-run mode - seen_jobs.json was NOT updated.", file=sys.stderr)

    # Output JSON to stdout for the scheduled Claude Code agent to consume
    print(json.dumps(matches, indent=2))


if __name__ == "__main__":
    main()
