import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

SEARCH_KEYWORDS = [
    "AI automation",
    "prompt engineering",
    "AI agent",
    "ChatGPT",
    "workflow automation",
    "n8n",
    "Zapier",
    "RPA",
    "Make.com",
    "LLM",
]

MATCH_THRESHOLD = 65  # minimum score (0-100) to include in digest

# Change to 24 when you're happy with the output and want the full daily window
MAX_POST_AGE_HOURS = 1

RECIPIENT_EMAIL = "you@example.com"

MODEL = "claude-sonnet-4-6"

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
RESUME_MD_PATH = os.path.join(DATA_DIR, "resume.md")   # editable source of truth
RESUME_PDF_PATH = os.path.join(DATA_DIR, "Resume_2026.pdf")
PROJECTS_PDF_PATH = os.path.join(DATA_DIR, "Past Projects _2026.pdf")
SEEN_JOBS_PATH = os.path.join(DATA_DIR, "seen_jobs.json")
