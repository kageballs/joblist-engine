"""Non-secret tunables.

Anything personal — rate floor, blocklists, resume — lives in profile.yaml,
which is gitignored. Nothing in this file should be embarrassing in public.
"""

# Scoring model. Bounded classification over short text against a fixed rubric,
# run daily forever, so the mid tier is the right default rather than the top one.
SCORING_MODEL = "claude-sonnet-5"
FAST_MODEL = "claude-haiku-4-5-20251001"

# Drafting a letter is a writing task over one long advert, not a ranking task
# over twelve short ones, so it does not share SCORE_BATCH_SIZE or its budget.
COVER_MODEL = SCORING_MODEL
COVER_MAX_TOKENS = 1200

# Hard ceiling on letters auto-drafted in one run, whatever the boards say.
# Per-board draft_at decides WHICH jobs qualify; this decides how much a very
# good day is allowed to cost. The rest stay available via `py cover.py <uid>`.
COVER_MAX_PER_RUN = 5

# Jobs per scoring request. Output size scales with this, so it is also the
# truncation guard: max_tokens is derived from it, not guessed.
SCORE_BATCH_SIZE = 12
# Raised from 220 when the scorer began returning `requirements` per job.
# Undersizing this does not lose data -- score_batch() bisects on max_tokens --
# but it pays for the discarded half of every overrun.
TOKENS_PER_JOB = 320

# How far back to look when there is no previous successful run to measure from.
FIRST_RUN_LOOKBACK_HOURS = 48
# Guards on the computed watermark: never re-read the whole board, never
# re-poll so tightly that a run costs more than it finds.
MIN_LOOKBACK_HOURS = 1
MAX_LOOKBACK_HOURS = 24 * 14

# Politeness. One run per day against a public endpoint is unimpeachable;
# a retry storm is not.
REQUEST_DELAY_SECONDS = 0.3
REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 3

HIMALAYAS_API = "https://himalayas.app/jobs/api"
# Himalayas caps page size at 20 no matter what `limit` asks for (measured
# 2026-08-24). Stated explicitly so nobody "optimises" it back up to 100.
HIMALAYAS_PAGE_SIZE = 20
HIMALAYAS_MAX_PAGES = 120

ONLINEJOBS_SEARCH = "https://www.onlinejobs.ph/jobseekers/jobsearch"
# Their robots.txt asks for Crawl-delay: 5 and we honour it literally. That is
# the whole politeness budget for this source -- 30 results per page means a
# 24h window is ~10 pages, so a run costs about a minute of wall clock.
ONLINEJOBS_CRAWL_DELAY_SECONDS = 5
ONLINEJOBS_PAGE_SIZE = 30
ONLINEJOBS_MAX_PAGES = 40
# Detail pages are fetched only for jobs that survive the funnel, one request
# each at the 5s crawl delay. The cap is a wall-clock guard: 120 survivors is
# already 10 minutes, and a run that would exceed it is better off truncated
# and loud than silently spending an hour.
ONLINEJOBS_MAX_HYDRATE = 120

# Indeed cannot be fetched by this process at all -- a plain `requests` GET is
# answered by a Cloudflare challenge on the first hit (docs/indeed-capture.md)
# -- so it is read from capture files a real browser session wrote to disk,
# never from the network. See sources/indeed.py.
INDEED_CAPTURE_DIR = "data/captures"
INDEED_SITE = "ph.indeed.com"
# A capture is a photograph of a search page, not a feed -- nothing refreshes
# it on its own. Past this age it contributes nothing and is skipped with a
# warning, because a silently ignored stale capture looks identical to a board
# with no jobs at all.
INDEED_MAX_CAPTURE_AGE_DAYS = 14

# OnlineJobs.ph states pay as free text, so the source normalises it to USD
# itself (sources/onlinejobs.py). These rates are approximate and were taken on
# 2026-08-30; they drift. That only matters for listings sitting within a few
# percent of the rate floor, which on this board is a rounding error against
# the ~96% that are nowhere near it. Revisit if PHP moves sharply.
FX_TO_USD = {"USD": 1.0, "PHP": 1 / 58.5, "AUD": 0.66}

# What a posting can ASK A CANDIDATE TO PRODUCE, as a fixed vocabulary.
#
# Fixed, because the whole point is counting across months of listings: if the
# model answers in free text, "screenshots of your GHL builds" and "portfolio
# of GoHighLevel work" are two rows and the tally is worthless. The model picks
# a key from this list and puts the specifics in `detail`.
#
# This is the ASK, not the person. Nothing here says what any candidate can or
# cannot supply -- that lives in profile.yaml and never leaves this machine.
REQUIREMENT_KINDS = {
    # ONE key, not two. Measured 2026-08-30 over 86 OLJ listings: employers ask
    # for "portfolio", "examples", "samples", "links to work you've done" and
    # almost never name a format. Splitting screenshots from links invented a
    # distinction the ads do not make, and a candidate whose client work is
    # under NDA cannot supply any of them regardless of format -- so the split
    # only produced a flag that matched nothing.
    "work_samples": "proof of work you have done, in any form: portfolio, samples, screenshots, live links, case studies",
    "public_code": "public GitHub/GitLab, or a code sample on request",
    "video_intro": "a recorded video or Loom introduction",
    "test_task": "an unpaid trial task, take-home or spec work",
    "timed_trial": "a paid or unpaid probation/trial period before hire",
    "certification": "a named vendor certification",
    "degree": "a formal degree",
    "years_experience": "a stated minimum number of years",
    "references": "contactable former employers or clients",
    "named_clients": "naming past clients or employers specifically",
    "own_tooling": "you must already own or pay for a specific tool licence",
    "equipment": "specific hardware, headset, or an internet speed floor",
    "time_tracker": "monitoring software or screenshot-based time tracking",
    "background_check": "police/NBI clearance or a background check",
    "id_verification": "verified government ID or platform ID proofing",
    "language_other": "a working language other than English",
    "onsite_presence": "any physical attendance, relocation or office days",
}

USER_AGENT = "joblist/2.0 (+https://github.com/kageballs/joblist)"

DATA_DIR = "data"
DB_PATH = "data/joblist.sqlite3"
DIGEST_DIR = "data/digest"
COVER_DIR = "data/covers"
PROFILE_PATH = "profile.yaml"
