import json
import sys
import anthropic
from config import ANTHROPIC_API_KEY, SEARCH_KEYWORDS, MATCH_THRESHOLD, MODEL
from agents.scraper import fetch_jobs, save_seen_ids

# ---------------------------------------------------------------------------
# Tool definitions exposed to Claude
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "search_jobs",
        "description": (
            "Fetch all new AI/automation job listings from Onlinejobs.ph. "
            "Call this ONCE — it already uses the configured keyword list internally. "
            "Returns a JSON array of job objects with fields: job_id, title, snippet, salary, url, posted_utc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "score_job_match",
        "description": (
            "Score how well a single job listing matches the candidate's resume (0-100). "
            "Call this for every job returned by search_jobs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "The job_id from search_jobs output"},
                "job_title": {"type": "string"},
                "job_description": {"type": "string", "description": "The snippet field from search_jobs output"},
            },
            "required": ["job_id", "job_title", "job_description"],
        },
    },
    {
        "name": "compile_digest",
        "description": (
            "Submit the final list of qualifying matches (score >= threshold). "
            "Call this once after scoring all jobs to finish the pipeline."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "matches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "job_id": {"type": "string"},
                            "title": {"type": "string"},
                            "url": {"type": "string"},
                            "salary": {"type": "string"},
                            "score": {"type": "integer"},
                            "reasoning": {"type": "string"},
                            "matched_skills": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                }
            },
            "required": ["matches"],
        },
    },
]

# ---------------------------------------------------------------------------
# Tool execution handlers
# ---------------------------------------------------------------------------

_fetched_jobs: dict[str, dict] = {}  # job_id -> job dict; shared across the run


def _handle_search_jobs() -> str:
    global _fetched_jobs
    new_jobs = fetch_jobs(SEARCH_KEYWORDS)
    for job in new_jobs:
        _fetched_jobs[job["job_id"]] = job
    return json.dumps(new_jobs)


def _handle_score_job_match(inputs: dict, resume_text: str, client: anthropic.Anthropic) -> str:
    job = _fetched_jobs.get(inputs["job_id"], {})
    salary = job.get("salary", "")
    salary_line = f"\nSALARY: {salary}" if salary else ""

    prompt = f"""Score how well this job fits the candidate's resume on a scale of 0-100.

JOB TITLE: {inputs['job_title']}{salary_line}

JOB DESCRIPTION:
{inputs['job_description']}

CANDIDATE RESUME:
{resume_text}

Reply ONLY with a JSON object — no markdown, no explanation:
{{"score": <0-100>, "reasoning": "<1-2 sentences>", "matched_skills": ["skill1", "skill2"]}}"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    # Strip accidental markdown fences
    raw = raw.strip("`").strip()
    if raw.startswith("json"):
        raw = raw[4:].strip()
    try:
        result = json.loads(raw)
        result["job_id"] = inputs["job_id"]
        result["title"] = inputs["job_title"]
        result["url"] = job.get("url", "")
        result["salary"] = job.get("salary", "")
        score = result.get("score", 0)
        print(f"[orchestrator]   score={score:3d}  {inputs['job_title'][:55]}", file=sys.stderr)
        return json.dumps(result)
    except json.JSONDecodeError:
        print(f"[orchestrator]   score=ERR  {inputs['job_title'][:55]}", file=sys.stderr)
        return json.dumps({
            "job_id": inputs["job_id"], "title": inputs["job_title"],
            "url": job.get("url", ""), "salary": job.get("salary", ""),
            "score": 0, "reasoning": "Parse error", "matched_skills": [],
        })


def _handle_compile_digest(inputs: dict) -> str:
    matches = inputs.get("matches", [])
    sorted_matches = sorted(matches, key=lambda x: x.get("score", 0), reverse=True)
    return json.dumps(sorted_matches)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a job-search agent. Evaluate AI/automation job listings from Onlinejobs.ph against a candidate's resume.

Strict workflow — follow in order:
1. Call search_jobs ONCE (no parameters). It returns a list of new job listings.
2. Call score_job_match for EVERY job in that list. Pass the job_id, title, and snippet as job_description.
3. After scoring ALL jobs, call compile_digest with only those that scored {threshold} or above.

Important rules:
- Do NOT call search_jobs more than once.
- Score every single job — do not skip any.
- The resume_text is NOT a parameter for score_job_match; scoring uses the resume you already have.
- End ONLY by calling compile_digest."""


# ---------------------------------------------------------------------------
# Main agentic loop
# ---------------------------------------------------------------------------

def run(resume_text: str, dry_run: bool = False) -> list[dict]:
    global _fetched_jobs
    _fetched_jobs = {}

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    messages = [
        {
            "role": "user",
            "content": (
                SYSTEM_PROMPT.format(threshold=MATCH_THRESHOLD)
                + f"\n\nCANDIDATE RESUME (use this when scoring):\n{resume_text}"
            ),
        }
    ]

    all_seen_ids: list[str] = []
    final_matches: list[dict] = []

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8096,
            tools=TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            break
        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            tool_input = block.input
            print(f"[orchestrator] -> {tool_name}", file=sys.stderr)

            if tool_name == "search_jobs":
                result_str = _handle_search_jobs()
                jobs_list = json.loads(result_str)
                all_seen_ids.extend(j["job_id"] for j in jobs_list)
                print(f"[orchestrator]    {len(jobs_list)} jobs returned", file=sys.stderr)

            elif tool_name == "score_job_match":
                result_str = _handle_score_job_match(tool_input, resume_text, client)

            elif tool_name == "compile_digest":
                result_str = _handle_compile_digest(tool_input)
                final_matches = json.loads(result_str)
                print(f"[orchestrator]    digest compiled: {len(final_matches)} matches", file=sys.stderr)

            else:
                result_str = json.dumps({"error": f"Unknown tool: {tool_name}"})

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str,
            })

        messages.append({"role": "user", "content": tool_results})

    if not dry_run and all_seen_ids:
        save_seen_ids(all_seen_ids)
        print(f"[orchestrator] Saved {len(all_seen_ids)} job IDs to seen_jobs.json", file=sys.stderr)

    return final_matches
