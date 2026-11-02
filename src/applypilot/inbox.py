"""Gmail response tracker (local patch, 2026-11-02).

Reads recent Gmail messages over IMAP (app password), keeps the ones that look
job-related, classifies them with the LLM, and then:
  * labels them in Gmail under "ApplyPilot/<Category>" (labels only: nothing is
    deleted, archived, moved or marked read),
  * sends a macOS notification for real responses (interviews, assessments,
    offers, requests for information, recruiter messages, rejections),
  * stores every decision in the `email_responses` table and rewrites
    ~/ApplyPilot/responses.md, a readable list of all responses, newest first.

Credentials go in ~/ApplyPilot/api_keys.env:
    GMAIL_ADDRESS=you@gmail.com
    GMAIL_APP_PASSWORD=abcd efgh ijkl mnop   (https://myaccount.google.com/apppasswords)
"""

from __future__ import annotations

import email
import imaplib
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from applypilot.config import APP_DIR
from applypilot.database import get_connection

log = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
LABEL_ROOT = "ApplyPilot"

CATEGORIES = {
    # category: (label, notify, title shown in the notification)
    "interview": ("Interview", True, "Interview invitation"),
    "assessment": ("Assessment", True, "Test / assessment"),
    "offer": ("Offer", True, "Offer"),
    "info_request": ("Action needed", True, "Action needed"),
    "recruiter_outreach": ("Recruiter", True, "Recruiter message"),
    "rejection": ("Rejected", True, "Rejection"),
    "confirmation": ("Applied", False, "Application received"),
    "job_alert": ("Job alerts", False, "Job alert"),
    "other": (None, False, "Other"),
}
POSITIVE = {"interview", "assessment", "offer", "info_request", "recruiter_outreach"}

# Applicant-tracking systems and job platforms that send application mail
ATS_DOMAINS = (
    "greenhouse.io", "lever.co", "myworkday", "workday.com", "smartrecruiters", "ashbyhq", "teamtailor",
    "personio", "recruitee", "workable", "successfactors", "sapsf", "icims", "taleo", "oraclecloud",
    "jobvite", "bamboohr", "breezy", "jazzhr", "join.com", "welcometothejungle", "free-work", "indeed",
    "linkedin.com", "softy.pro", "flatchr", "talent-soft", "cornerstoneondemand", "avature", "phenom",
    "pinpointhq", "homerun", "hrmos", "jobteaser", "stepstone", "infojobs", "pracuj", "glassdoor",
)
KEYWORDS = re.compile(
    r"application|applied|candidate|candidacy|interview|assessment|recruit|position|vacanc|hiring|"
    r"offer letter|unfortunately|next steps|candidature|entretien|postul|recrutement|candidatura|"
    r"colloquio|selezion|bewerbung|vorstellungsgespr|absage|sollicitatie|candidatura|entrevista|"
    r"rekrutacj|aplikacj|ansökan|ansøgning|hakemus",
    re.I,
)

CLASSIFY_PROMPT = """You sort a job seeker's email. The candidate ({name}) has applied to many jobs.
Classify the email below. Return ONLY JSON:
{{"job_related": true/false, "category": one of {cats}, "company": "employer name or empty",
  "role": "job title if stated or empty", "summary": "one short sentence in English",
  "action": "what the candidate should do, or empty",
  "followup": one of ["verify_link", "complete_application", "consent", "optional", "human", "none"],
  "link": number of the link to open for the follow-up (from LINKS below), or 0}}

FOLLOW-UP means what an automated assistant should do next:
- verify_link: click a verification / confirmation / activation / "sign in to continue" link that is valid for hours or days
- complete_application: the application is incomplete or not yet submitted; log in to the portal and finish it
- consent: give consent to data processing / privacy notice so the application can proceed
- optional: an optional survey, newsletter choice, profile tip or marketing preference; nothing required
- human: a decision only the candidate can take (interview scheduling, assessment or test, offer, salary negotiation, questions about them)
- none: nothing to do (confirmations, rejections, alerts, codes that expire within minutes)
An email asking to verify an account/email, to continue, finalize or complete an application, to sign in to continue, or to give
consent gets category info_request. A one-time code that expires within minutes is category other, followup none.

Categories:
- confirmation: automatic "we received your application" / "thank you for applying"
- rejection: they will not move forward
- interview: invitation to talk, schedule a call or interview, or a recruiter asking for availability
- assessment: a test, coding task, questionnaire, video interview or case study to complete
- offer: a job offer
- info_request: they ask for documents, details, salary, availability or confirmation before continuing, OR the application is incomplete / must be finalized or submitted ("finalisez votre candidature", "complete your application", "your application is incomplete")
- recruiter_outreach: a recruiter or company reaching out about a role the candidate did not apply to
- job_alert: newsletters, job recommendations, platform notifications
- other: job-related but none of the above (account verification codes, password resets, etc.)
Set job_related=false for anything unrelated to job hunting.

EMAIL
From: {sender}
Subject: {subject}
Date: {date}

{body}

LINKS
{links}
"""


# ── storage ──────────────────────────────────────────────────────────────

def _ensure_table(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS email_responses (
            message_id TEXT PRIMARY KEY, received_at TEXT, sender TEXT, subject TEXT,
            job_related INTEGER, category TEXT, company TEXT, role TEXT, summary TEXT,
            action TEXT, job_url TEXT, notified INTEGER DEFAULT 0, processed_at TEXT)"""
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(email_responses)").fetchall()}
    for col in ("handled INTEGER DEFAULT 0", "followup TEXT", "followup_link TEXT", "followup_status TEXT", "followup_note TEXT"):
        if col.split()[0] not in cols:
            conn.execute(f"ALTER TABLE email_responses ADD COLUMN {col}")
    conn.commit()


def _match_job(conn, company: str, role: str) -> str | None:
    """Best-effort link to an application in the jobs table."""
    for needle in (company, role):
        needle = (needle or "").strip()
        if len(needle) < 3:
            continue
        row = conn.execute(
            "SELECT url FROM jobs WHERE apply_status='applied' AND (title LIKE ? OR full_description LIKE ? "
            "OR application_url LIKE ?) ORDER BY applied_at DESC LIMIT 1",
            (f"%{needle}%", f"%{needle}%", f"%{needle.lower().replace(' ', '')}%"),
        ).fetchone()
        if row:
            return row[0]
    return None


# ── email helpers ────────────────────────────────────────────────────────

def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _body_text(msg: email.message.Message, limit: int = 2500) -> str:
    plain, html = "", ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/plain" and not plain:
            plain = text
        elif ctype == "text/html" and not html:
            html = text
    if not plain and html:
        try:
            from bs4 import BeautifulSoup
            plain = BeautifulSoup(html, "html.parser").get_text(" ")
        except Exception:
            plain = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", plain).strip()[:limit]


_SKIP_LINK = re.compile(r"unsubscribe|mailto:|privacy-policy|facebook\.com|twitter\.com|x\.com/|instagram|linkedin\.com/company|youtube|/legal|desabonn|abbestellen", re.I)


def _extract_links(msg: email.message.Message, plain: str, limit: int = 15) -> list[tuple[str, str]]:
    """(anchor text, url) pairs from the HTML part, plus bare URLs from the text."""
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in msg.walk():
        if part.get_content_type() != "text/html" or part.get_filename():
            continue
        try:
            html = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", errors="replace")
            from bs4 import BeautifulSoup
            for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
                url = a["href"].strip()
                text = re.sub(r"\s+", " ", a.get_text(" ")).strip()[:60]
                if url.startswith("http") and url not in seen and not _SKIP_LINK.search(url + " " + text):
                    seen.add(url)
                    links.append((text or "(image/button)", url))
        except Exception:
            continue
    for url in re.findall(r"https?://[^\s<>\"')\]]+", plain or ""):
        url = url.rstrip(".,;")
        if url not in seen and not _SKIP_LINK.search(url):
            seen.add(url)
            links.append(("(in text)", url))
    return links[:limit]


def _looks_job_related(sender: str, subject: str, body: str) -> bool:
    domain = parseaddr(sender)[1].split("@")[-1].lower()
    if any(d in domain for d in ATS_DOMAINS):
        return True
    return bool(KEYWORDS.search(subject) or KEYWORDS.search(body[:1500]))


AUTO_FOLLOWUPS = ("verify_link", "complete_application", "consent")


def _classify(sender: str, subject: str, date: str, body: str, name: str, links: list[tuple[str, str]] | None = None) -> dict:
    from applypilot.llm import get_client
    links = links or []
    link_text = "\n".join(f"{i + 1}. [{t}] {u}" for i, (t, u) in enumerate(links)) or "(none)"
    prompt = CLASSIFY_PROMPT.format(name=name, cats=list(CATEGORIES), sender=sender,
                                    subject=subject, date=date, body=body, links=link_text)
    raw = get_client().ask(prompt, max_tokens=500, temperature=0.0)
    match = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(match.group(0)) if match else {}
    if data.get("category") not in CATEGORIES:
        data["category"] = "other"
    if data.get("followup") not in ("verify_link", "complete_application", "consent", "optional", "human", "none"):
        data["followup"] = "human" if data["category"] == "info_request" else "none"
    try:
        idx = int(data.get("link") or 0)
        data["followup_link"] = links[idx - 1][1] if 0 < idx <= len(links) else ""
    except Exception:
        data["followup_link"] = ""
    if data["followup"] in AUTO_FOLLOWUPS and data["category"] not in ("info_request", "other"):
        data["followup"] = "none"
    return data


def notify(title: str, message: str, sound: bool = True) -> None:
    """macOS notification (no-op elsewhere)."""
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{esc(message)[:220]}" with title "{esc(title)[:80]}"'
    if sound:
        script += ' sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception:
        pass


# ── main entry ───────────────────────────────────────────────────────────

def check_inbox(days: int = 3, label: bool = True, notify_user: bool = True, dry_run: bool = False,
                max_messages: int = 300) -> list[dict]:
    """Process Gmail messages from the last `days` days. Returns the newly classified job emails."""
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if not address or not password:
        raise RuntimeError("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in ~/ApplyPilot/api_keys.env "
                           "(app password: https://myaccount.google.com/apppasswords)")
    from applypilot.config import load_profile
    name = load_profile().get("personal", {}).get("full_name", "the candidate")

    conn = get_connection()
    _ensure_table(conn)
    seen = {r[0] for r in conn.execute("SELECT message_id FROM email_responses")}

    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    imap.login(address, password)
    try:
        imap.select("INBOX", readonly=dry_run or not label)
        typ, data = imap.search(None, "X-GM-RAW", f'"newer_than:{int(days)}d -in:chats"')
        ids = (data[0].split() if typ == "OK" and data and data[0] else [])[-max_messages:]
        # 1) fetch candidates (IMAP is sequential), 2) classify in parallel, 3) label/notify/store
        candidates: list[tuple[bytes, dict, str]] = []
        for num in reversed(ids):  # newest first
            typ, hdr = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])")
            if typ != "OK" or not hdr or not isinstance(hdr[0], tuple):
                continue
            h = email.message_from_bytes(hdr[0][1])
            mid = (h.get("Message-ID") or f"imap-{num.decode()}").strip()
            if mid in seen:
                continue
            sender, subject, date_raw = _decode(h.get("From")), _decode(h.get("Subject")), h.get("Date", "")
            try:
                received = parsedate_to_datetime(date_raw).astimezone(timezone.utc).isoformat(timespec="minutes")
            except Exception:
                received = date_raw
            typ, full = imap.fetch(num, "(BODY.PEEK[]<0.120000>)")  # PEEK: never marks as read
            parsed = email.message_from_bytes(full[0][1]) if typ == "OK" and full and isinstance(full[0], tuple) else None
            body = _body_text(parsed) if parsed else ""
            links = _extract_links(parsed, body) if parsed else []
            row = {"message_id": mid, "received_at": received, "sender": sender, "subject": subject,
                   "job_related": 0, "category": None, "company": "", "role": "", "summary": "",
                   "action": "", "job_url": None, "notified": 0, "followup": None, "followup_link": None,
                   "followup_status": None, "followup_note": None, "handled": 0, "_links": links}
            candidates.append((num, row, body))

        from concurrent.futures import ThreadPoolExecutor

        def work(item):
            num, row, body = item
            if not _looks_job_related(row["sender"], row["subject"], body):
                return item, None
            try:
                return item, _classify(row["sender"], row["subject"], row["received_at"], body, name, row.get("_links"))
            except Exception as exc:
                log.warning("Classification failed for %r: %s", row["subject"][:60], exc)
                return item, "error"

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(work, candidates))

        new: list[dict] = []
        for (num, row, _body), c in results:
            if c == "error":
                continue  # retry next time
            if c:
                row.update(job_related=int(bool(c.get("job_related"))), category=c.get("category"),
                           company=c.get("company", ""), role=c.get("role", ""),
                           summary=c.get("summary", ""), action=c.get("action", ""))
                if row["job_related"]:
                    row["job_url"] = _match_job(conn, row["company"], row["role"])
                    row["followup"] = c.get("followup", "none")
                    row["followup_link"] = c.get("followup_link") or None
                    if row["followup"] in AUTO_FOLLOWUPS:
                        row["followup_status"] = "pending"   # ApplyPilot will handle it (applypilot followups)
                    elif row["followup"] == "optional":
                        row["handled"] = 1
                        row["followup_status"] = "skipped"
                        row["followup_note"] = "Optional; nothing required, skipped automatically."
                    lbl, should_notify, title = CATEGORIES[row["category"]]
                    if label and lbl and not dry_run:
                        imap.store(num, "+X-GM-LABELS", f'("{LABEL_ROOT}/{lbl}")')
                    # only disturb the user for things an assistant cannot do
                    if should_notify and notify_user and not dry_run and row["followup"] in ("human", "none"):
                        who = row["company"] or parseaddr(row["sender"])[0] or parseaddr(row["sender"])[1]
                        notify(f"{title}: {who}", row["summary"] or row["subject"], sound=row["category"] in POSITIVE)
                        row["notified"] = 1
                    new.append(row)
            if not dry_run:
                conn.execute(
                    "INSERT OR REPLACE INTO email_responses (message_id, received_at, sender, subject, job_related, "
                    "category, company, role, summary, action, job_url, notified, processed_at, handled, followup, "
                    "followup_link, followup_status, followup_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["message_id"], row["received_at"], row["sender"], row["subject"], row["job_related"],
                     row["category"], row["company"], row["role"], row["summary"], row["action"], row["job_url"],
                     row["notified"], datetime.now(timezone.utc).isoformat(timespec="seconds"), row.get("handled", 0),
                     row.get("followup"), row.get("followup_link"), row.get("followup_status"), row.get("followup_note")),
                )
                conn.commit()
        return new
    finally:
        try:
            imap.logout()
        except Exception:
            pass
        if not dry_run:
            write_digest()


def write_digest() -> str:
    """Rewrite ~/ApplyPilot/responses.md with every job response, newest first."""
    conn = get_connection()
    _ensure_table(conn)
    rows = conn.execute(
        "SELECT received_at, category, company, role, summary, action, subject, sender, message_id "
        "FROM email_responses WHERE job_related=1 AND category NOT IN ('confirmation','job_alert','other') "
        "ORDER BY received_at DESC"
    ).fetchall()
    counts = dict(conn.execute(
        "SELECT category, count(*) FROM email_responses WHERE job_related=1 GROUP BY category").fetchall())
    lines = ["# Responses from companies", "",
             f"Updated {datetime.now().strftime('%Y-%m-%d %H:%M')}. "
             + ", ".join(f"{CATEGORIES.get(k, (k,))[0] or k}: {v}" for k, v in sorted(counts.items())), ""]
    for r in rows:
        when, cat, company, role, summary, action, subject, sender, mid = r
        link = f"https://mail.google.com/mail/u/0/#search/rfc822msgid%3A{mid.strip('<>')}"
        lines += [f"## {CATEGORIES.get(cat, (cat,))[0] or cat}: {company or sender}" + (f" ({role})" if role else ""),
                  f"- {str(when)[:16].replace('T', ' ')} | {subject}", f"- {summary}"]
        if action:
            lines.append(f"- To do: {action}")
        lines += [f"- [Open in Gmail]({link})", ""]
    path = APP_DIR / "responses.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)
