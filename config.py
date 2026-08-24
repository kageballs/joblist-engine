"""Non-secret tunables.

Anything personal — rate floor, blocklists, resume — lives in profile.yaml,
which is gitignored. Nothing in this file should be embarrassing in public.
"""

# Scoring model. Bounded classification over short text against a fixed rubric,
# run daily forever, so the mid tier is the right default rather than the top one.
SCORING_MODEL = "claude-sonnet-5"
FAST_MODEL = "claude-haiku-4-5-20251001"

# Jobs per scoring request. Output size scales with this, so it is also the
# truncation guard: max_tokens is derived from it, not guessed.
SCORE_BATCH_SIZE = 12
TOKENS_PER_JOB = 220

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

USER_AGENT = "joblist/2.0 (+https://github.com/kageballs/joblist)"

DATA_DIR = "data"
DB_PATH = "data/joblist.sqlite3"
DIGEST_DIR = "data/digest"
PROFILE_PATH = "profile.yaml"
