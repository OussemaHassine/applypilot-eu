"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot import pause
from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.lang import OTHER_LANG_NAMES, detect_other_language
from applypilot.llm import get_client

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

# The rubric is written for one candidate: {years}, {languages} and the derived values are filled
# in from profile.json at runtime (see _system_prompt), so nothing personal lives in the code.
SCORE_PROMPT = """You are a job fit evaluator for one specific candidate: a software QA / test-automation engineer with about {years} years of professional experience, who reads and works in {languages}. Given the candidate's resume and a job posting, score how good a target the posting is for THIS candidate to apply to. A high score means "apply now"; a low score means "do not bother".

CANDIDATE FACTS: about {years} years of professional software QA experience; highest degree: {education}; working languages: {languages}. Trust these over any assumption from dates in the resume.

HARD CAPS (check these first; the lowest applicable cap wins; name the cap in the reasoning):
1. Posting language: a description written mainly in a language other than {languages} (German, Dutch, Spanish, Portuguese, Polish, Swedish, Danish, Norwegian, Finnish, Hungarian, Czech, ...): at most 3. A bilingual posting that carries a complete version in one of {languages} is fine.
2. Role: the job must be software QA / software testing / test automation / SDET / QA automation / software quality engineering, in any title wording (QA Engineer, Software Tester, Testeur, Ingénieur QA, Ingénieur validation logiciel, Recette fonctionnelle, Collaudo software, Quality Assurance Analyst, Test Lead who still tests...). Testing AI/LLM features counts as QA. Anything else (developer, DevOps/SRE, data, product, support, business analyst, project manager, hardware/firmware/embedded test benches, pharma, manufacturing or automotive quality, network/telecom test): at most 3; at most 5 when the role's core work clearly transfers from the candidate's (test automation, scripting, API work, CI pipelines, release or test tooling, QA-adjacent analysis).
3. Internships, apprenticeships, PhD positions, student, graduate-programme or entry-level-only roles (stage, tirocinio, stagiaire, alternance, Werkstudent, Praktikum, intern): at most 3.
4. People management or very senior roles (Head of QA, QA/Test Manager, Engineering Manager, Director, Principal, Staff) or any role asking for {years_plus5}+ years: at most 5. A "Senior QA Engineer" asking {years_plus2} years is NOT in this group.
5. A must-have the candidate cannot meet, limited to three cases: fluency in a language outside {languages}; a specific citizenship or nationality (including a security clearance the posting ties to nationality); or the posting says visa sponsorship is not offered for a job where the candidate would need it ({sponsorship}): at most 4. Any other requirement he lacks (a certification, a degree, a tool) is an ordinary gap, never a cap.

YEARS OF EXPERIENCE: the candidate has about {years} years. A posting asking up to 2 years more ({years_plus1} or {years_plus2}) is still a full match on experience: take off at most one point and never let it block a 7. A posting asking 3 or 4 years more: take off two points. Seniority words alone ("senior", "confirmé", "experienced") are not a gap.

SCORE (after the caps):
- 9-10: hands-on QA / test-automation role matching the candidate's stack (Playwright or similar UI automation, Python/TypeScript test code, API testing, CI pipelines, Agile QA process, test strategy) with no real gap.
- 7-8: QA / test-automation role where the candidate covers most requirements and the gaps are easy to bridge: another automation framework (Selenium, Cypress, Robot, Java/C#/Kotlin test code), another business domain, mobile or performance testing as a nice-to-have, a manual QA role with some automation.
- 5-6: QA role with one key requirement genuinely missing: a mandatory specialism (performance, security, mobile-native, SAP/ERP, games, embedded) or a mandatory tool he never used that is the core of the job.
- 3-4: capped by a rule above, or QA-adjacent at best.
- 1-2: unrelated field.

Count in favour: Playwright, Python, TypeScript/JavaScript, API testing (REST, Postman), CI/CD (GitLab CI, GitHub Actions), Agile/Scrum, test strategy and planning, exploratory and regression testing, AI-assisted testing, remote or hybrid work, English-speaking teams.
Count against: purely manual roles with no automation at all (7 at most); a certification, degree or tool he lacks (ordinary gap). Location in Europe or the Gulf is never a gap by itself: the candidate is open to relocation.

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences in English, whatever the posting's language: which cap applied if any, the main match, the main gap]"""

DEFAULT_POSTING_LANGUAGES = ["English", "French", "Italian"]
_SYSTEM_PROMPT: str | None = None


def _posting_languages() -> str:
    """"English, French or Italian": the languages a posting may be written in (profile
    languages.posting_languages, else the default)."""
    langs = list((load_profile() or {}).get("languages", {}).get("posting_languages") or DEFAULT_POSTING_LANGUAGES)
    return ", ".join(langs[:-1]) + " or " + langs[-1] if len(langs) > 1 else langs[0]


def _system_prompt() -> str:
    """SCORE_PROMPT with the candidate's years and posting languages filled in from the profile."""
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        try:
            years = int((load_profile() or {}).get("experience", {}).get("years_of_experience_total") or 3)
        except (TypeError, ValueError):
            years = 3
        profile = load_profile() or {}
        education = profile.get("experience", {}).get("education_level") or "Master's degree"
        sponsorship = (profile.get("work_authorization", {}).get("sponsorship_rule")
                       or "sponsorship is needed for on-site or hybrid jobs outside the candidate's country")
        _SYSTEM_PROMPT = SCORE_PROMPT.format(years=years, years_plus1=years + 1, years_plus2=years + 2,
                                             years_plus5=years + 5, languages=_posting_languages(),
                                             education=education, sponsorship=sponsorship)
    return _SYSTEM_PROMPT


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    reasoning = response

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    # Postings clearly written in a language the candidate does not read are not wanted:
    # decide here and save the model call (bilingual or unclear texts still go to the model).
    other = detect_other_language(job.get("full_description"))
    if other:
        return {"score": 2, "keywords": "",
                "reasoning": f"Cap 1: the posting is written in {OTHER_LANG_NAMES[other]}; only postings in "
                             f"{_posting_languages()} are wanted."}

    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client()
        response = client.chat(messages, max_tokens=512, temperature=0.2)
        return _parse_score_response(response)
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}"}


def run_scoring(limit: int = 0, rescore: bool = False) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    log.info("Scoring %d jobs sequentially...", len(jobs))
    t0 = time.time()
    completed = 0
    errors = 0
    results: list[dict] = []

    for job in jobs:
        pause.wait_if_paused("pipeline")
        result = score_job(resume_text, job)
        result["url"] = job["url"]
        completed += 1

        if result["score"] == 0:
            errors += 1

        results.append(result)

        log.info(
            "[%d/%d] score=%d  %s",
            completed, len(jobs), result["score"], job.get("title", "?")[:60],
        )

        # Write each score immediately so downstream stages (and a crash) never
        # lose a whole pass, and so the write lock is held for microseconds.
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
            (result["score"], f"{result['keywords']}\n{result['reasoning']}",
             datetime.now(timezone.utc).isoformat(), result["url"]),
        )
        conn.commit()

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed, len(results) / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
