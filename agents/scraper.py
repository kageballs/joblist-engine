import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

from config import SEEN_JOBS_PATH, MAX_POST_AGE_HOURS

BASE_URL = "https://www.onlinejobs.ph/jobseekers/jobsearch"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _load_seen_ids() -> set:
    try:
        with open(SEEN_JOBS_PATH, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _post_age_hours(utc_str: str) -> float | None:
    """
    Parse data-temp-2 attribute value (UTC datetime string) and return age in hours.
    Returns None if unparseable.
    """
    if not utc_str:
        return None
    try:
        posted = datetime.strptime(utc_str.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - posted).total_seconds() / 3600.0
    except ValueError:
        return None


def _is_recent(utc_str: str, max_hours: float) -> bool:
    age = _post_age_hours(utc_str)
    if age is None:
        return True  # include if timestamp is unparseable
    return age <= max_hours


def _parse_jobs_from_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs = []

    for card in soup.select("div.latest-job-post"):
        # Title — strip any badge text (part-time, full-time spans)
        title_el = card.select_one("h4")
        if not title_el:
            continue
        for badge in title_el.select("span"):
            badge.decompose()
        title = title_el.get_text(strip=True)

        # Job link and ID
        link_el = card.select_one("a[href*='/jobseekers/job/']")
        if not link_el:
            continue
        href = link_el.get("href", "")
        job_id_match = re.search(r"/job/(\d+)", href)
        if not job_id_match:
            continue
        job_id = job_id_match.group(1)
        url = f"https://www.onlinejobs.ph{href}" if href.startswith("/") else href

        # UTC posted timestamp from data attribute
        time_el = card.select_one("p[data-temp-2]")
        posted_utc = time_el["data-temp-2"] if time_el else ""

        # Description snippet
        desc_el = card.select_one("div.desc")
        snippet = ""
        if desc_el:
            for a in desc_el.select("a"):
                a.decompose()
            snippet = desc_el.get_text(strip=True)[:400]

        # Salary hint
        salary_el = card.select_one("dd")
        salary = salary_el.get_text(strip=True) if salary_el else ""

        jobs.append({
            "job_id": job_id,
            "title": title,
            "company": "Unknown",  # not shown on search results cards
            "snippet": snippet,
            "salary": salary,
            "url": url,
            "posted_utc": posted_utc,
        })

    return jobs


def fetch_jobs(keywords: list[str]) -> list[dict]:
    seen_ids = _load_seen_ids()
    all_jobs: dict[str, dict] = {}
    total_found = 0
    total_old = 0

    for keyword in keywords:
        try:
            resp = requests.get(
                BASE_URL,
                params={"search": keyword},
                headers=HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
            parsed = _parse_jobs_from_html(resp.text)
            total_found += len(parsed)

            for job in parsed:
                jid = job["job_id"]
                if jid in seen_ids or jid in all_jobs:
                    continue
                if not _is_recent(job["posted_utc"], MAX_POST_AGE_HOURS):
                    total_old += 1
                    continue
                all_jobs[jid] = job

            time.sleep(2)
        except requests.RequestException as e:
            print(f"[scraper] Warning: failed to fetch '{keyword}': {e}", file=sys.stderr)

    print(
        f"[scraper] {total_found} listings across all keywords | "
        f"{total_old} older than {MAX_POST_AGE_HOURS}h | "
        f"{len(all_jobs)} new jobs to score",
        file=sys.stderr,
    )
    return list(all_jobs.values())


def save_seen_ids(job_ids: list[str]) -> None:
    existing = _load_seen_ids()
    merged = list(existing | set(job_ids))
    with open(SEEN_JOBS_PATH, "w") as f:
        json.dump(merged, f)
