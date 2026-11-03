"""Cover letter generation: LLM-powered, profile-driven, with validation.

Generates concise, engineering-voice cover letters tailored to specific job
postings. All personal data (name, skills, achievements) comes from the user's
profile at runtime. No hardcoded personal information.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot.config import COVER_LETTER_DIR, load_profile, load_resume_text
from applypilot.location import localize_resume_header, resolve_candidate_location
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.lang import (
    COVER_SALUTATIONS,
    DEFAULT_LANG,
    LANG_NAMES,
    SALUTATION_PREFIXES,
    detect_job_language,
)
from applypilot.llm import get_client
from applypilot.scoring.validator import (
    BANNED_WORDS,
    LLM_LEAK_PHRASES,
    sanitize_text,
    validate_cover_letter,
    find_new_numbers,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


# ── Prompt Builder (profile-driven) ──────────────────────────────────────

def _build_cover_letter_prompt(profile: dict, lang: str = DEFAULT_LANG, location: dict | None = None) -> str:
    """Build the cover letter system prompt from the user's profile.

    All personal data, skills, and sign-off name come from the profile.
    """
    personal = profile.get("personal", {})
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Preferred name for the sign-off (falls back to full name)
    sign_off_name = personal.get("preferred_name") or personal.get("full_name", "")
    full_name = personal.get("full_name") or sign_off_name

    # Flatten all allowed skills
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "the tools listed in the resume"

    # Real metrics from resume_facts
    real_metrics = resume_facts.get("real_metrics", [])
    preserved_projects = resume_facts.get("preserved_projects", [])

    # Build achievement examples for the prompt
    projects_hint = ""
    if preserved_projects:
        projects_hint = f"\nKnown projects to reference: {', '.join(preserved_projects)}"

    metrics_hint = ""
    if real_metrics:
        metrics_hint = f"\nReal metrics to use: {', '.join(real_metrics)}"

    # Build the full banned list from the validator so the prompt stays in sync
    # with what will actually be rejected — the validator checks all of these.
    all_banned = ", ".join(f'"{w}"' for w in BANNED_WORDS)
    leak_banned = ", ".join(f'"{p}"' for p in LLM_LEAK_PHRASES)
    lang_name = LANG_NAMES.get(lang, "English")
    salutation = COVER_SALUTATIONS.get(lang, COVER_SALUTATIONS["en"])
    location_line = ""
    if location:
        from applypilot.location import spoken_languages_for
        arabic_rule = "" if location.get("mention_arabic") else " Never mention Arabic."
        location_line = (
            f"\nLOCATION: {sign_off_name} is currently based in {location['label']} and open to relocation anywhere "
            f"in Europe and to remote roles. Mention this only if the posting makes location relevant; never name another city.\n"
            f"LANGUAGES: {spoken_languages_for(location, lang)}.{arabic_rule}\n"
        )

    return f"""Write a cover letter for {sign_off_name}. The goal is to get an interview.

LANGUAGE: Write the entire letter in {lang_name}, the language of the job posting. Keep tool, product and company names as they are. Use natural {lang_name} business-letter phrasing, including the closing line.

STRUCTURE: 3 short paragraphs. Under 250 words. Every sentence must earn its place.
{location_line}

PARAGRAPH 1 (2-3 sentences): Open with a specific thing YOU built that solves THEIR problem. Not "I'm excited about this role." Not "This role aligns with my experience." Start with the work.

PARAGRAPH 2 (3-4 sentences): Pick 2 achievements from the resume that are MOST relevant to THIS job. Use a number only if it appears in the resume; if an achievement has no number, describe it without one. Frame as solving their problem, not listing your accomplishments.{projects_hint}{metrics_hint}

PARAGRAPH 3 (1-2 sentences): One specific thing about the company from the job description (a product, a technical challenge, a team structure). Then close. "Happy to walk through any of this in more detail." or "Let's discuss." Nothing else.

BANNED WORDS AND PHRASES (automated validator rejects ANY of these — do not use even once):
{all_banned}

ALSO BANNED (meta-commentary the validator catches):
{leak_banned}

BANNED PUNCTUATION: No em dashes (—) or en dashes (–). Use commas or periods.

VOICE:
- Write like a real engineer emailing someone they respect. Not formal, not casual. Just direct.
- NEVER narrate or explain what you're doing. BAD: "This demonstrates my commitment to X." GOOD: Just state the fact and move on.
- NEVER hedge. BAD: "might address some of your challenges." GOOD: "solves the same problem your team is facing."
- Every sentence should contain a tool name from the resume, a real outcome from the resume, or a specific point from the job description. If it doesn't, cut it.
- Read it out loud. If it sounds like a robot wrote it, rewrite it.

FABRICATION = INSTANT REJECTION:
The candidate's real tools are ONLY: {skills_str}.
Do NOT mention ANY tool not in this list. If the job asks for tools not listed, talk about the work you did, not the tools.
Every claim about the candidate must be traceable to a sentence in the resume: no invented projects, clients, team sizes, outcomes, numbers, certifications or motivations. Say nothing about the company beyond what the job description states. Do not claim the candidate has used, led or delivered anything the resume does not say.

Sign off with the full name: "{full_name}"

Output ONLY the letter text. No subject lines. No "Here is the cover letter:" preamble. No notes after the sign-off.
Start DIRECTLY with "{salutation}" and end with "{full_name}"."""


# ── Helpers ──────────────────────────────────────────────────────────────

def _strip_preamble(text: str, lang: str = DEFAULT_LANG) -> str:
    """Remove LLM preamble before the salutation if present.

    Gemini and other models sometimes output "Here is the cover letter:" or
    similar meta-commentary before the actual letter text. Strip everything
    before the first salutation for the letter's language so the validator's
    start-check passes.
    """
    lowered = text.lower()
    prefixes = SALUTATION_PREFIXES.get(lang, SALUTATION_PREFIXES["en"])
    positions = [lowered.find(p) for p in prefixes]
    positions = [i for i in positions if i >= 0]
    if positions and min(positions) > 0:
        return text[min(positions):]
    return text


# ── Core Generation ──────────────────────────────────────────────────────

def generate_cover_letter(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
    lang: str = DEFAULT_LANG,
) -> str:
    """Generate a cover letter with fresh context on each retry + auto-sanitize.

    Same design as tailor_resume: fresh conversation per attempt, issues noted
    in the prompt, no conversation history stacking.

    Args:
        resume_text:      The candidate's resume text (base or tailored).
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".

    Returns:
        The cover letter text (best attempt even if validation failed).
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    avoid_notes: list[str] = []
    letter = ""
    client = get_client()
    location = resolve_candidate_location(profile, job, lang)
    resume_text = localize_resume_header(resume_text, location["label"])
    cl_prompt_base = _build_cover_letter_prompt(profile, lang, location)

    for attempt in range(max_retries + 1):
        # Fresh conversation every attempt
        prompt = cl_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES:\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (
                f"RESUME:\n{resume_text}\n\n---\n\n"
                f"TARGET JOB:\n{job_text}\n\n"
                "Write the cover letter:"
            )},
        ]

        letter = client.chat(messages, max_tokens=1024, temperature=0.7)
        letter = sanitize_text(letter)  # auto-fix em dashes, smart quotes
        letter = _strip_preamble(letter, lang)  # remove any "Here is the letter:" prefix

        validation = validate_cover_letter(letter, mode=validation_mode, lang=lang)
        if validation["passed"] and validation_mode != "lenient":
            # Programmatic fabrication check: numbers must come from the resume or the posting
            new_nums = find_new_numbers(letter, resume_text + "\n" + job_text)
            if new_nums:
                validation = {
                    "passed": False,
                    "errors": ["Numbers not in the resume or job posting: " + ", ".join(new_nums[:8])
                               + ". Use only numbers that appear in the resume."],
                    "warnings": [],
                }
        if validation["passed"]:
            return letter

        avoid_notes.extend(validation["errors"])
        # Warnings never block — only hard errors trigger a retry
        log.debug(
            "Cover letter attempt %d/%d failed: %s",
            attempt + 1, max_retries + 1, validation["errors"],
        )

    if validation_mode == "strict":
        log.warning("Cover letter rejected after %d attempts: %s", max_retries + 1, avoid_notes[-2:])
        return ""  # strict mode: no letter is better than an unverified one
    return letter  # last attempt even if failed


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_cover_letters(min_score: int = 7, limit: int = 20,
                      validation_mode: str = "normal") -> dict:
    """Generate cover letters for high-scoring jobs that have tailored resumes.

    Args:
        min_score:       Minimum fit_score threshold.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", or "lenient".

    Returns:
        {"generated": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    resume_cache: dict[str, str] = {}  # base resume text per language (en / fr / it)
    conn = get_connection()

    # Fetch jobs that have tailored resumes but no cover letter yet
    jobs = conn.execute(
        "SELECT * FROM jobs "
        "WHERE fit_score >= ? AND tailored_resume_path IS NOT NULL "
        "AND full_description IS NOT NULL "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
        "AND COALESCE(cover_attempts, 0) < ? "
        "ORDER BY fit_score DESC" + (" LIMIT ?" if limit and limit > 0 else ""),
        (min_score, MAX_ATTEMPTS, limit) if limit and limit > 0 else (min_score, MAX_ATTEMPTS),
    ).fetchall()

    if not jobs:
        log.info("No jobs needing cover letters (score >= %d).", min_score)
        return {"generated": 0, "errors": 0, "elapsed": 0.0}

    # Convert rows to dicts
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    log.info(
        "Generating cover letters for %d jobs (score >= %d)...",
        len(jobs), min_score,
    )
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    error_count = 0

    for job in jobs:
        completed += 1
        try:
            lang = detect_job_language(job)
            if lang not in resume_cache:
                resume_cache[lang] = load_resume_text(lang).replace("**", "")
            letter = generate_cover_letter(resume_cache[lang], job, profile,
                                          validation_mode=validation_mode, lang=lang)
            if not letter.strip():
                raise ValueError("cover letter failed validation after all retries")

            # Build safe filename prefix
            safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
            safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
            prefix = f"{safe_site}_{safe_title}"

            cl_path = COVER_LETTER_DIR / f"{prefix}_CL.txt"
            cl_path.write_text(letter, encoding="utf-8")

            # Generate PDF (best-effort)
            pdf_path = None
            try:
                from applypilot.scoring.pdf import convert_to_pdf
                pdf_path = str(convert_to_pdf(cl_path, lang=lang))
            except Exception:
                log.debug("PDF generation failed for %s", cl_path, exc_info=True)

            result = {
                "url": job["url"],
                "path": str(cl_path),
                "pdf_path": pdf_path,
                "title": job["title"],
                "site": job["site"],
            }
            results.append(result)

            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            log.info(
                "%d/%d [OK] lang=%s | %.1f jobs/min | %s",
                completed, len(jobs), lang, rate * 60, result["title"][:40],
            )
        except Exception as e:
            result = {
                "url": job["url"], "title": job["title"], "site": job["site"],
                "path": None, "pdf_path": None, "error": str(e),
            }
            error_count += 1
            results.append(result)
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), job["title"][:40], e)

    # Persist to DB: increment attempt counter for ALL, save path only for successes
    now = datetime.now(timezone.utc).isoformat()
    saved = 0
    for r in results:
        if r.get("path"):
            conn.execute(
                "UPDATE jobs SET cover_letter_path=?, cover_letter_at=?, "
                "cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                (r["path"], now, r["url"]),
            )
            saved += 1
        else:
            conn.execute(
                "UPDATE jobs SET cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                (r["url"],),
            )
    conn.commit()

    elapsed = time.time() - t0
    log.info("Cover letters done in %.1fs: %d generated, %d errors", elapsed, saved, error_count)

    return {
        "generated": saved,
        "errors": error_count,
        "elapsed": elapsed,
    }


def generate_for_job(url: str, validation_mode: str = "strict",
                     dest_dir: "str | None" = None) -> dict:
    """Write the cover letter for ONE job, on demand (local patch 2026-11-03).

    `applypilot cover-letter --url ...` calls this; the apply agent runs that command only when
    the application form actually has a cover-letter field or upload, so letters are no longer
    produced in bulk for every scored job. Reuses an existing letter when one was already written.

    Returns {"txt": path, "pdf": path | None, "text": letter, "lang": lang}.
    """
    import shutil
    from pathlib import Path as _P

    conn = get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE url=?", (url,)).fetchone()
    if not row:
        raise ValueError(f"no job with url {url}")
    job = dict(row)
    profile = load_profile()
    lang = detect_job_language(job)

    existing = job.get("cover_letter_path")
    if existing and _P(existing).exists():
        cl_path = _P(existing)
        letter = cl_path.read_text(encoding="utf-8")
        pdf = cl_path.with_suffix(".pdf")
        pdf_path = str(pdf) if pdf.exists() else None
    else:
        if not job.get("full_description"):
            raise ValueError("job has no full description yet (run enrich first)")
        letter = generate_cover_letter(load_resume_text(lang).replace("**", ""), job, profile,
                                       validation_mode=validation_mode, lang=lang)
        if not letter.strip():
            conn.execute("UPDATE jobs SET cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?", (url,))
            conn.commit()
            raise ValueError("cover letter failed validation after all retries")
        safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
        safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
        COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
        import hashlib
        tag = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]   # same title at the same site must not share a file
        cl_path = COVER_LETTER_DIR / f"{safe_site}_{safe_title}_{tag}_CL.txt"
        cl_path.write_text(letter, encoding="utf-8")
        pdf_path = None
        try:
            from applypilot.scoring.pdf import convert_to_pdf
            pdf_path = str(convert_to_pdf(cl_path, lang=lang))
        except Exception:
            log.debug("PDF generation failed for %s", cl_path, exc_info=True)
        conn.execute(
            "UPDATE jobs SET cover_letter_path=?, cover_letter_at=?, "
            "cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
            (str(cl_path), datetime.now(timezone.utc).isoformat(), url),
        )
        conn.commit()

    if pdf_path and dest_dir:
        name = profile["personal"]["full_name"].replace(" ", "_")
        dest = _P(dest_dir) / f"{name}_Cover_Letter.pdf"
        shutil.copy(pdf_path, dest)
        pdf_path = str(dest)
    return {"txt": str(cl_path), "pdf": pdf_path, "text": letter, "lang": lang}
