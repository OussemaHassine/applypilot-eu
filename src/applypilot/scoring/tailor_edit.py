"""Edit-in-place tailoring (local patch, 2026-11-03).

User rule: the tailored CV keeps the exact format of the base CV (resume_<lang>.txt) and its text
is changed ONLY when needed for the job. The LLM therefore edits the base text instead of
rewriting a resume from JSON, and the result is checked line by line against the base:

  may change : the title line under the name, the profile paragraph, the order of bullets /
               projects / skills items, and the wording of at most MAX_REWORDED_BULLETS bullets
  must match : name and contact lines, section headings, every "A | B | C | D" entry line,
               skills labels and items, the number of bullets per entry, **bold** markers

`tailor.tailor_resume` dispatches here unless APPLYPILOT_TAILOR_MODE=rewrite.
"""

from __future__ import annotations

import logging
import re

from applypilot.lang import DEFAULT_LANG, LANG_NAMES
from applypilot.llm import get_client
from applypilot.location import localize_resume_header, resolve_candidate_location
from applypilot.scoring.validator import (
    BANNED_WORDS, LLM_LEAK_PHRASES, _build_skills_set, find_new_numbers, find_unknown_skills, sanitize_text,
)

log = logging.getLogger(__name__)

MAX_REWORDED_BULLETS = 4
_HEADING_RE = re.compile(r"^[A-ZÀ-ÝŒ][A-ZÀ-ÝŒ &'’/-]{3,}$")
_SENIORITY = ("senior", "lead", "staff", "principal", "head", "manager", "director", "chief", "sr.", "sr ")


def plain(text: str) -> str:
    return (text or "").replace("**", "")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", plain(s)).strip().lower().rstrip(".")


def structure(text: str) -> dict:
    """Header lines, ordered sections, entry lines, bullets and skills of a base-format CV."""
    lines = [l.rstrip() for l in (text or "").strip().splitlines()]
    header: list[str] = []
    i = 0
    while i < len(lines) and len(header) < 3:
        if lines[i].strip():
            header.append(lines[i].strip())
        i += 1
    sections: list[dict] = []
    cur: dict | None = None
    for l in lines[i:]:
        s = l.strip()
        if not s:
            continue
        if _HEADING_RE.match(s) and not s.startswith("-"):
            cur = {"heading": s, "entries": [], "paragraph": [], "skills": []}
            sections.append(cur)
            continue
        if cur is None:
            continue
        if s.startswith("- ") or s.startswith("• "):
            if cur["entries"]:
                cur["entries"][-1]["bullets"].append(s[2:].strip())
            else:
                cur["paragraph"].append(s)
        elif " | " in s:
            cur["entries"].append({"line": s, "bullets": []})
        elif re.match(r"^[^:]{2,32}\s?:", s) and not cur["entries"]:
            label, val = s.split(":", 1)
            cur["skills"].append((label.strip(), [x.strip() for x in val.split(",") if x.strip()]))
        else:
            cur["paragraph"].append(s)
    return {"name": header[0] if header else "", "title": header[1] if len(header) > 1 else "",
            "contact": header[2] if len(header) > 2 else "", "sections": sections}


def check_structure(base: dict, new: dict) -> tuple[list[str], int]:
    """Errors (for the retry prompt) + number of reworded bullets."""
    errors: list[str] = []
    if _norm(new["name"]) != _norm(base["name"]):
        errors.append("Line 1 (the name) must stay identical.")
    if _norm(new["contact"]) != _norm(base["contact"]):
        errors.append("Line 3 (the contact line) must stay identical.")
    bt, nt = base["title"].lower(), new["title"].lower()
    added = [w for w in _SENIORITY if w in nt and w not in bt]
    if added:
        errors.append(f"The title line added a seniority word ({added[0].strip()}); the base CV has none.")
    if [s["heading"] for s in new["sections"]] != [s["heading"] for s in base["sections"]]:
        errors.append("Section headings must stay identical, in the same order: "
                      + " / ".join(s["heading"] for s in base["sections"]))
        return errors, 0
    base_bullets = {_norm(b) for s in base["sections"] for e in s["entries"] for b in e["bullets"]}
    reworded = 0
    for bs, ns in zip(base["sections"], new["sections"]):
        b_lines = [e["line"] for e in bs["entries"]]
        n_lines = [e["line"] for e in ns["entries"]]
        if bs["entries"] and [ _norm(x) for x in n_lines ] != [ _norm(x) for x in b_lines ]:
            if sorted(_norm(x) for x in n_lines) == sorted(_norm(x) for x in b_lines) and not any(
                    e["bullets"] for e in bs["entries"]) is False and bs["heading"] and "PROJ" in bs["heading"].upper():
                pass  # projects may be reordered
            else:
                errors.append(f"In {bs['heading']}, every entry line (A | B | C | D) must stay identical and in the same "
                              f"order (only projects may be reordered). Expected: " + " ; ".join(b_lines))
                continue
        b_by = { _norm(e["line"]): e for e in bs["entries"] }
        for ne in ns["entries"]:
            be = b_by.get(_norm(ne["line"]))
            if be is None:
                continue
            if len(ne["bullets"]) != len(be["bullets"]):
                errors.append(f"'{plain(be['line'])[:50]}' must keep exactly {len(be['bullets'])} bullet(s); got {len(ne['bullets'])}.")
            for b in ne["bullets"]:
                if _norm(b) not in base_bullets:
                    reworded += 1
                if "**" in b and b.count("**") % 2:
                    errors.append("Unbalanced ** bold markers in a bullet.")
        if bs["skills"]:
            if [l for l, _ in ns["skills"]] != [l for l, _ in bs["skills"]]:
                errors.append("Skills labels must stay identical: " + ", ".join(l for l, _ in bs["skills"]))
            else:
                for (lab, bi), (_, ni) in zip(bs["skills"], ns["skills"]):
                    if sorted(_norm(x) for x in bi) != sorted(_norm(x) for x in ni):
                        errors.append(f"Skills under '{lab}' must be the same items (reorder only): " + ", ".join(bi))
    if reworded > MAX_REWORDED_BULLETS:
        errors.append(f"{reworded} bullets were reworded; reword at most {MAX_REWORDED_BULLETS}, and only where "
                      "the posting's vocabulary makes it necessary. Keep the other bullets exactly as in the base CV.")
    return errors, reworded


def _edit_prompt(profile: dict, lang: str, banned: str) -> str:
    name = profile.get("personal", {}).get("full_name", "the candidate")
    return f"""You tailor {name}'s CV for one job by EDITING the base CV text. Return the complete CV in EXACTLY the same format (same lines, same order, same markers) and change text ONLY where this job needs it. The CV is written in {LANG_NAMES.get(lang, 'English')}; keep that language.

YOU MAY CHANGE
1. Line 2, the title under the name: use the posting's role wording when it matches the candidate's real work (QA Engineer, QA Automation Engineer, Test Automation Engineer, SDET, Software Tester, Test Analyst...). Never add Senior, Lead, Staff, Principal, Manager or any seniority the base CV does not have. If the posting is not a QA/testing role, keep the base title.
2. The paragraph under the first heading (the profile): rewrite it for this job in 2-4 sentences. Every claim must be backed by a bullet in the base CV. Keep the sentence about relocation/remote if the base has one.
3. Order only: bullets inside a job, the order of projects, the order of items inside a skills category (the posting's must-haves first).
4. The wording of at most {MAX_REWORDED_BULLETS} bullets, and only where the posting's vocabulary or emphasis makes it necessary (for example the posting says "acceptance testing" and the bullet says "UAT"). Same facts, same numbers, same tools, similar length. Keep the **bold** markers on phrases that remain.

EVERYTHING ELSE STAYS BYTE-FOR-BYTE IDENTICAL: the name line, the contact line, every section heading, every "A | B | C | D" entry line (employers, roles, locations, dates, schools, degrees, project names, tech lists), the skills labels and their items, the number of bullets under each entry, the bold markers. Never add or drop a bullet, skill, tool, number, certification or project. Never invent anything.

VOICE: direct engineer voice, no buzzwords. Banned words (never use): {banned}. No em dashes.

OUTPUT: the full CV text only. No code fences, no commentary, no explanations."""


def _judge_prompt(profile: dict, lang: str) -> str:
    boundary = profile.get("skills_boundary", {})
    skills = ", ".join(x for v in boundary.values() if isinstance(v, list) for x in v) or "N/A"
    return f"""You are a resume quality judge. A tailoring step EDITED the candidate's own CV for one job. Catch LIES, not style.

Answer with EXACTLY this format:
VERDICT: PASS or FAIL
ISSUES: (list, or "none")

ALLOWED (never fail for these): a different title line under the name; a rewritten profile paragraph; reordered bullets, projects or skills items; up to {MAX_REWORDED_BULLETS} reworded bullets describing the same work; the CV being written in {LANG_NAMES.get(lang, 'English')}.

FABRICATION (FAIL): a tool, language, framework, platform or certification that is not in the ORIGINAL (allowed skills: {skills}); a number not in the ORIGINAL; a claim in the profile or a bullet with no basis in the ORIGINAL; a changed employer, role, date, school or degree; an added seniority level; a certification "in progress" presented as obtained.

Be strict about facts only."""


def _clean(raw: str) -> str:
    text = (raw or "").strip()
    if "```" in text:
        parts = text.split("```")
        text = max(parts[1::2], key=len).strip() if len(parts) > 1 else text
        if text.lower().startswith(("text", "markdown", "plaintext")):
            text = text.split("\n", 1)[1] if "\n" in text else ""
    text = "\n".join(sanitize_text(l) if l.strip() else "" for l in text.splitlines())
    return text.strip() + "\n"


def tailor_resume_edit(resume_text: str, job: dict, profile: dict, max_retries: int = 3,
                       validation_mode: str = "normal", lang: str = DEFAULT_LANG) -> tuple[str, dict]:
    """Same contract as tailor.tailor_resume: returns (tailored_text, report)."""
    job_text = (f"TITLE: {job['title']}\nCOMPANY: {job['site']}\nLOCATION: {job.get('location', 'N/A')}\n\n"
                f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}")
    location = resolve_candidate_location(profile, job, lang)
    base = localize_resume_header(resume_text, location["label"])
    base_s = structure(base)
    banned = ", ".join(sorted(BANNED_WORDS)[:60])
    system = _edit_prompt(profile, lang, banned)
    report: dict = {"attempts": 0, "validator": None, "judge": None, "status": "pending",
                    "validation_mode": validation_mode, "language": lang, "mode": "edit", "reworded_bullets": 0}
    avoid: list[str] = []
    client = get_client()
    text = base
    for attempt in range(max_retries + 1):
        report["attempts"] = attempt + 1
        prompt = system + ("\n\n## FIX THESE ISSUES FROM THE PREVIOUS ATTEMPT:\n" + "\n".join(f"- {n}" for n in avoid[-6:]) if avoid else "")
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": f"BASE CV:\n{base}\n\n---\n\nTARGET JOB:\n{job_text}\n\nReturn the full edited CV text:"}]
        text = _clean(client.chat(messages, max_tokens=3000, temperature=0.3))

        errors, reworded = check_structure(base_s, structure(text))
        report["reworded_bullets"] = reworded
        if validation_mode != "lenient":
            nums = find_new_numbers(plain(text), plain(base))
            if nums:
                errors.append("Numbers not in the base CV: " + ", ".join(nums[:8]) + ". Use only numbers from the base CV.")
            skills = {lab: ", ".join(items) for s in structure(text)["sections"] for lab, items in s["skills"]}
            unknown = find_unknown_skills(skills, _build_skills_set(profile), plain(base)) if skills else []
            if unknown:
                errors.append("Skills not in the base CV: " + ", ".join(unknown[:8]) + ". Remove them.")
        low = plain(text).lower()
        banned_hits = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", low)
                       and not re.search(r"\b" + re.escape(w) + r"\b", plain(base).lower())]
        if banned_hits and validation_mode == "strict":
            errors.append("Banned words: " + ", ".join(banned_hits[:5]))
        leaks = [p for p in LLM_LEAK_PHRASES if p in low]
        if leaks:
            errors.append(f"Remove commentary: '{leaks[0]}'")
        report["validator"] = {"passed": not errors, "errors": errors,
                               "warnings": [f"banned words (not blocking): {', '.join(banned_hits[:5])}"] if banned_hits and not errors else []}
        if errors:
            avoid.extend(errors)
            if attempt < max_retries:
                continue
            report["status"] = "failed_validation"
            return text, report

        if validation_mode == "lenient":
            report["judge"] = {"verdict": "SKIPPED", "passed": True, "issues": "none"}
            report["status"] = "approved"
            return text, report
        jm = [{"role": "system", "content": _judge_prompt(profile, lang)},
              {"role": "user", "content": f"JOB TITLE: {job.get('title', '')}\n\nORIGINAL RESUME:\n{plain(base)}\n\n---\n\nTAILORED RESUME:\n{plain(text)}\n\nJudge this tailored resume:"}]
        resp = client.chat(jm, max_tokens=400, temperature=0.1)
        passed = "VERDICT: PASS" in resp.upper()
        issues = resp[resp.upper().index("ISSUES:") + 7:].strip() if "ISSUES:" in resp.upper() else "none"
        report["judge"] = {"passed": passed, "verdict": "PASS" if passed else "FAIL", "issues": issues, "raw": resp}
        if not passed:
            avoid.append(f"Judge rejected: {issues}")
            if attempt < max_retries:
                continue
            report["status"] = "approved_with_judge_warning"
            return text, report
        report["status"] = "approved"
        return text, report
    report["status"] = "exhausted_retries"
    return text, report
