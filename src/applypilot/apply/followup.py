"""Email follow-ups handled by the browser agent (local patch, 2026-11-02).

`applypilot inbox` marks job emails that an assistant can act on
(`email_responses.followup` in verify_link / complete_application / consent,
`followup_status = 'pending'`). This module opens them in the worker Chrome and
lets the Claude agent do the step: click the verification link, log in and
finish an incomplete application, or give consent. Interviews, assessments,
offers and anything needing judgment stay with the human.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from applypilot import config, pause
from applypilot.apply import prompt as prompt_mod
from applypilot.apply.chrome import cleanup_worker, launch_chrome, reset_worker_dir
from applypilot.apply.launcher import BASE_CDP_PORT, _CLI_PAUSE_MARKERS, _make_mcp_config
from applypilot.database import get_connection
from applypilot.inbox import AUTO_FOLLOWUPS, notify

log = logging.getLogger(__name__)

WORKER_ID = 9            # own Chrome profile/port, so it never collides with apply workers 0-3
TIMEOUT = 480            # seconds per follow-up
HUMAN_REASONS = ("interview", "assessment", "test", "offer", "video", "payment", "identity")

TASKS = {
    "verify_link": (
        "Open the link below. It should verify the candidate's email or account (or sign them in to continue). "
        "If the page then shows an application that is not finished, complete and submit it exactly like a normal application "
        "(upload the resume PDF, fill every required field from the profile, answer screening questions by the rules). "
        "If the link has expired, request a new one from the same page when possible, read it from Gmail and continue."
    ),
    "complete_application": (
        "The employer says the application is incomplete or not yet submitted. Open the link below (or the employer's career "
        "site), sign in with the candidate's email and password (reset the password through Gmail if the site rejects it), "
        "open the unfinished application for the role named below, complete every required step and submit it. "
        "Upload the resume PDF if a resume is missing."
    ),
    "consent": (
        "The employer asks the candidate to consent to the processing of their personal data (privacy notice) so the "
        "application can proceed. Open the link, read what is asked, and accept the data-processing consent needed for this "
        "recruitment. Decline any optional marketing or newsletter consent."
    ),
}


def _pending(limit: int) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM email_responses WHERE followup_status='pending' AND followup IN (?,?,?) "
        "ORDER BY received_at DESC LIMIT ?", (*AUTO_FOLLOWUPS, limit)).fetchall()
    return [dict(r) for r in rows]


def _job_for(row: dict) -> dict | None:
    if not row.get("job_url"):
        return None
    r = get_connection().execute("SELECT * FROM jobs WHERE url=?", (row["job_url"],)).fetchone()
    return dict(r) if r else None


def _build_prompt(row: dict, job: dict | None, profile: dict, worker_dir: Path) -> str:
    personal = profile["personal"]
    # resume to upload: the tailored one for the linked job, else the base English resume
    src_pdf = None
    if job and job.get("tailored_resume_path"):
        cand = Path(job["tailored_resume_path"]).with_suffix(".pdf")
        src_pdf = cand if cand.exists() else None
    if src_pdf is None:
        src_pdf = config.RESUME_PDF_PATH if config.RESUME_PDF_PATH.exists() else None
    pdf_path = "N/A"
    if src_pdf:
        dest = worker_dir / f"{personal['full_name'].replace(' ', '_')}_Resume.pdf"
        shutil.copy(src_pdf, dest)
        pdf_path = str(dest)

    from applypilot.config import load_blocked_sso
    blocked_sso = ", ".join(load_blocked_sso())
    task = TASKS[row["followup"]]
    role_line = f"Role: {job['title']} ({job.get('location') or ''})\nPosting: {job.get('application_url') or job['url']}" if job else \
        f"Role: {row.get('role') or 'see email'}"
    return f"""You are an autonomous browser agent finishing a job-application follow-up for {personal['full_name']}.

== THE EMAIL ==
From: {row['sender']}
Subject: {row['subject']}
Summary: {row['summary']}
What it asks: {row['action']}
Employer: {row.get('company') or 'unknown'}
{role_line}
Link to open: {row.get('followup_link') or '(none given: find the way in through the employer career site)'}

== YOUR TASK ({row['followup']}) ==
{task}

== FILES ==
Resume PDF (upload if a resume is required): {pdf_path}

== APPLICANT PROFILE ==
{prompt_mod._build_profile_summary(profile)}

== CREDENTIALS & LOGIN ==
- Employer career sites: sign in with {personal['email']} / {personal.get('password', '')} (if the site limits the length, use the first 16 characters).
- If sign-in fails and the account exists: use "Forgot password", open the candidate's Gmail in a NEW tab (https://mail.google.com/mail/u/0/#inbox,
  already signed in), open the newest reset email from this site, set the password to the one above, sign in. Never reset Google,
  Microsoft, LinkedIn or Gmail passwords, and never type a Google password anywhere.
- Verification code needed? Read it from Gmail the same way. Only open the message that belongs to this employer.
- If you land on {blocked_sso} or any Google/Microsoft single sign-on page: do not use it. Output RESULT:FAILED:sso_required.

{prompt_mod._build_hard_rules(profile)}

== NEVER DO THESE ==
- Never do video/audio interviews, selfie or ID verification, assessments or tests -> RESULT:NEEDS_HUMAN:<what it is>
- Never schedule or confirm interview slots, accept or negotiate offers -> RESULT:NEEDS_HUMAN:<what it is>
- Never enter payment details. Never install anything. Never grant camera/microphone permissions.
- Never invent facts about the candidate; a required fact missing from the profile -> RESULT:FAILED:missing_required_<fact>

{prompt_mod._build_captcha_section()}

== STEPS ==
1. browser_navigate to the link. browser_snapshot. Handle cookie banners.
2. Do the task. If it turns into an application form, complete it fully and submit; check every pre-filled field against the profile.
3. After submitting, snapshot and make sure the page confirms success.
4. Output exactly one result line:
RESULT:DONE -- the follow-up is complete (verified / submitted / consented)
RESULT:NEEDS_HUMAN:<reason> -- only the candidate can do this
RESULT:FAILED:<reason> -- could not finish (expired link, login impossible, missing fact, site error)
Keep thinking short. Stop after the result line."""


def _run_agent(prompt: str, port: int, worker_dir: Path, model: str) -> tuple[str, str]:
    mcp = config.APP_DIR / f".mcp-followup-{WORKER_ID}.json"
    mcp.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")
    cmd = ["claude", "--model", model, "-p", "--mcp-config", str(mcp), "--permission-mode", "bypassPermissions",
           "--no-session-persistence", "--output-format", "text", "-"]
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT, env=env, cwd=str(worker_dir))
        out = (proc.stdout or "") + ("\n" + proc.stderr if proc.returncode else "")
    except subprocess.TimeoutExpired:
        return "FAILED:timeout", "timed out"
    low = out.lower()
    if any(m in low for m in _CLI_PAUSE_MARKERS):
        return "PAUSED", out[-500:]
    m = re.search(r"RESULT:(DONE|NEEDS_HUMAN(?::[^\n]*)?|FAILED(?::[^\n]*)?)", out)
    return (m.group(1).strip() if m else "FAILED:no_result_line"), out


def run_followups(limit: int = 10, headless: bool | None = None, model: str = "sonnet") -> dict:
    """Handle pending automatable follow-ups. Returns counts."""
    config.load_env()
    profile = config.load_profile()
    pending = _pending(limit)
    stats = {"done": 0, "needs_human": 0, "failed": 0, "paused": 0, "total": len(pending)}
    if not pending:
        log.info("No pending follow-ups.")
        return stats
    log.info("Handling %d follow-up(s)...", len(pending))
    conn = get_connection()
    port = BASE_CDP_PORT + WORKER_ID
    log_path = config.LOG_DIR / "followups.log"
    for row in pending:
        pause.wait_if_paused("followups")
        job = _job_for(row)
        worker_dir = reset_worker_dir(WORKER_ID)
        prompt = _build_prompt(row, job, profile, worker_dir)
        chrome = None
        t0 = time.time()
        try:
            hl = headless if headless is not None else config.headless_setting()
            chrome = launch_chrome(WORKER_ID, port=port, headless=hl)
            result, out = _run_agent(prompt, port, worker_dir, model)
        except Exception as exc:
            result, out = f"FAILED:{exc}", str(exc)
        finally:
            if chrome:
                cleanup_worker(WORKER_ID, chrome)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n{'=' * 60}\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {row['followup']} | {row.get('company')} | {row['subject']}\n"
                     f"link: {row.get('followup_link')}\n{'=' * 60}\n{out[-6000:]}\nRESULT -> {result}\n")
        elapsed = int(time.time() - t0)
        if result == "PAUSED":
            stats["paused"] += 1
            log.warning("Claude unavailable (usage limit); stopping follow-ups for now.")
            break
        kind = result.split(":", 1)[0]
        note = result.split(":", 1)[1] if ":" in result else ""
        status = {"DONE": "done", "NEEDS_HUMAN": "needs_human"}.get(kind, "failed")
        conn.execute("UPDATE email_responses SET followup_status=?, followup_note=?, handled=? WHERE message_id=?",
                     (status, (note or kind.lower())[:300] + f" ({elapsed}s)", 1 if status == "done" else 0, row["message_id"]))
        conn.commit()
        stats[status] += 1
        log.info("%s -> %s %s", (row.get("company") or row["subject"])[:40], status.upper(), note[:60])
        if status != "done":
            notify(f"Follow-up needs you: {row.get('company') or row['subject'][:40]}", (note or row["summary"])[:200], sound=True)
    return stats
