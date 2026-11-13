"""Cover letter generation: LLM-powered, profile-driven, with validation.

Generates full-length (about 300 words), human-sounding cover letters tailored to
specific job postings. All personal data (name, skills, achievements) comes from the user's
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
    COVER_LETTER_TELLS,
    COVER_TARGET_WORDS,
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
    tells_banned = ", ".join(f'"{p}"' for p in COVER_LETTER_TELLS)
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

    return f"""Write a cover letter for {sign_off_name}. The reader is a recruiter or hiring manager who goes through dozens of these a day, and the goal is an interview. It has to read like {sign_off_name} sat down and wrote it for this one job, not like a template with the company name swapped in.

LANGUAGE: Write the entire letter in {lang_name}, the language of the job posting. Keep tool, product and company names as they are. Use natural {lang_name} business-letter phrasing, including the closing line.

LENGTH AND SHAPE: 4 paragraphs, {COVER_TARGET_WORDS[0]} to {COVER_TARGET_WORDS[1]} words in total. Plain paragraphs separated by a blank line. No bullets, no headings, no bold.
LAYOUT: "{salutation}" alone on the first line, a blank line, the 4 paragraphs, a blank line, a short closing formula alone on its own line (the usual one for a {lang_name} letter, e.g. the equivalent of "Kind regards,"), then "{full_name}" alone on the last line. The closing formula is never part of paragraph 4.
{location_line}
EMPLOYER NAME: The employer is the company the job description names. "FOUND ON" is only the job board (LinkedIn, Indeed, HelloWork...) and must never be written as the employer or mentioned at all. If the description does not name the employer (anonymous or agency posting), write "your team" / "your company" and do not guess a name.

PARAGRAPH 1 (2-3 sentences): Name the role and the employer, then go straight to the part of your work that sits closest to what this team needs and say why that made the posting stand out to you. Not "I am writing to apply for". Not "I'm excited about this role".

PARAGRAPH 2 (4-5 sentences): Tell the story of the ONE achievement from the resume that matters most for this job: what you were dealing with, what you did, how you went about it, and what changed afterwards. Give it room. Use a number only if it appears in the resume; if the resume gives no number, describe the result without one.{projects_hint}{metrics_hint}

PARAGRAPH 3 (3-4 sentences): A second piece of experience that answers a DIFFERENT requirement of the posting. Say which requirement it answers, in your own words, so the reader sees you read the posting properly.

PARAGRAPH 4 (3-4 sentences): What draws you to this company or team, based on one concrete thing the job description says (a product, a technical challenge, how the team works) and tied to work you have actually done. If location is relevant, give it one short sentence of its own here. Then close simply and warmly by offering a conversation, in one sentence.

HOW A PERSON WRITES (this matters as much as the content):
- First person, plain everyday words, the way a thoughtful engineer writes to a future colleague. Warm and direct. Not stiff, not gushing.
- Mix sentence lengths. Follow a long sentence with a short one. Never start more than two sentences in a row with "I".
- Retell the resume, do not copy it. A resume bullet pasted into a sentence is the fastest way to sound generated. Explain the work the way you would across a table.
- Connect ideas the way people do in speech ("That is why", "Before that", "In practice", "What I liked about that work") and let one paragraph lead into the next.
- In English a few contractions (I've, I'm, didn't) are welcome. In other languages keep the normal register of a professional letter.
- Not every sentence needs a tool name. One or two sentences of plain reasoning about why you worked that way make the letter sound like a person.
- No semicolons. No lists of three adjectives. No "not only X but also Y". No rhetorical questions. No sentence that sums up the paragraph it sits in. No sentence that explains what the previous one "shows" or "demonstrates".
- Do not hand the job description back to the reader in its own sentences. Refer to one or two concrete things from it in your own words.
- Do not hedge ("might", "could potentially", "I think I may"). State what you did and what you would bring.

BANNED WORDS AND PHRASES (automated validator rejects ANY of these — do not use even once):
{all_banned}

ALSO BANNED (stock phrases that mark a letter as machine-written or copy-pasted):
{tells_banned}

ALSO BANNED (meta-commentary the validator catches):
{leak_banned}

BANNED PUNCTUATION: No em dashes (—) or en dashes (–), no semicolons. Use commas or periods.

FABRICATION = INSTANT REJECTION:
The candidate's real tools are ONLY: {skills_str}.
Do NOT mention ANY tool not in this list. If the job asks for tools not listed, talk about the work you did, not the tools.
Every claim about the candidate must be traceable to a sentence in the resume: no invented projects, clients, team sizes, outcomes, numbers, certifications, anecdotes or personal history. The extra length must come from explaining real work in more depth and from linking it to the posting, never from new facts. If the resume does not give a detail, leave it out. Say nothing about the company beyond what the job description states. Do not claim the candidate has used, led or delivered anything the resume does not say.

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
        f"FOUND ON (the job board or source; this is NOT the employer): {job['site']}\n"
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

        letter = client.chat(messages, max_tokens=2048, temperature=0.8)
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
