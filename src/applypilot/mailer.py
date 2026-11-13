"""Application emails over Gmail SMTP (local patch, 2026-11-03).

Some postings have no form: "send your CV to jobs@company.com". The apply agent
writes the message and runs `applypilot send-email`, which sends it from the
candidate's Gmail address with the tailored CV attached. It uses the same
GMAIL_ADDRESS / GMAIL_APP_PASSWORD pair as `applypilot inbox` (api_keys.env), so
replies land in the inbox the response tracker already reads.

Guard rails, because an agent is the caller:
  * recipients must be real addresses, at most MAX_RECIPIENTS (+ cc);
  * attachments must be existing PDFs, at most MAX_ATTACHMENTS / MAX_TOTAL_BYTES;
  * one email per job URL: logs/sent_emails.jsonl is the ledger and a second
    call for the same URL is refused unless force=True;
  * dry_run builds the message and sends nothing.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from applypilot import config

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
LEDGER_PATH = config.LOG_DIR / "sent_emails.jsonl"

MAX_RECIPIENTS = 3
MAX_ATTACHMENTS = 5
MAX_TOTAL_BYTES = 15 * 1024 * 1024
MIN_BODY_CHARS = 500   # about 90 words; shorter reads as a throwaway note

_ADDRESS_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")


class EmailError(Exception):
    """Anything that stops the email from being sent; the message is safe to show the agent."""


def credentials() -> tuple[str, str]:
    """Gmail address and app password from the environment (api_keys.env)."""
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if not address or not password:
        raise EmailError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD are not set in api_keys.env")
    return address, password


def is_configured() -> bool:
    try:
        credentials()
        return True
    except EmailError:
        return False


def _clean_addresses(values, what: str, limit: int) -> list[str]:
    out: list[str] = []
    for raw in values or ():
        for part in str(raw).replace(";", ",").split(","):
            addr = part.strip().strip("<>")
            if not addr:
                continue
            if not _ADDRESS_RE.match(addr):
                raise EmailError(f"{what} address does not look valid: {addr!r}")
            if addr.lower() not in {a.lower() for a in out}:
                out.append(addr)
    if len(out) > limit:
        raise EmailError(f"too many {what} addresses ({len(out)}); at most {limit}")
    return out


def _check_attachments(paths) -> list[Path]:
    files: list[Path] = []
    total = 0
    for raw in paths or ():
        p = Path(raw).expanduser()
        if not p.is_file():
            raise EmailError(f"attachment not found: {p}")
        if p.suffix.lower() != ".pdf":
            raise EmailError(f"only PDF attachments are sent: {p.name}")
        total += p.stat().st_size
        files.append(p)
    if len(files) > MAX_ATTACHMENTS:
        raise EmailError(f"too many attachments ({len(files)}); at most {MAX_ATTACHMENTS}")
    if total > MAX_TOTAL_BYTES:
        raise EmailError(f"attachments are {total // (1024 * 1024)} MB; the limit is {MAX_TOTAL_BYTES // (1024 * 1024)} MB")
    return files


def already_sent(job_url: str | None) -> dict | None:
    """The ledger record of an email already sent for this job URL, if any."""
    if not job_url or not LEDGER_PATH.exists():
        return None
    for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("job_url") == job_url:
            return rec
    return None


def sent_emails() -> list[dict]:
    """Every ledger record, oldest first."""
    if not LEDGER_PATH.exists():
        return []
    out = []
    for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def build_message(sender: str, sender_name: str, to: list[str], subject: str, body: str,
                  attachments: list[Path], cc: list[str] | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender)) if sender_name else sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject.strip()
    msg["Message-ID"] = make_msgid(domain=sender.split("@", 1)[-1])
    msg.set_content(body.strip() + "\n")
    for p in attachments:
        msg.add_attachment(p.read_bytes(), maintype="application", subtype="pdf", filename=p.name)
    return msg


def send_application_email(to, subject: str, body: str, attachments=(), job_url: str | None = None,
                           cc=(), dry_run: bool = False, force: bool = False) -> dict:
    """Validate, send (unless dry_run) and record one application email.

    Returns the ledger record (with "dry_run": True when nothing was sent).
    Raises EmailError for anything the caller can fix; SMTP failures surface as
    EmailError too, with the server's answer in the text.
    """
    recipients = _clean_addresses(to, "recipient", MAX_RECIPIENTS)
    if not recipients:
        raise EmailError("no recipient address given")
    copies = _clean_addresses(cc, "cc", MAX_RECIPIENTS)
    files = _check_attachments(attachments)
    if not subject or not subject.strip():
        raise EmailError("subject is empty")
    if len((body or "").strip()) < MIN_BODY_CHARS:
        raise EmailError(f"body is too short ({len((body or '').strip())} chars, minimum {MIN_BODY_CHARS}); "
                         "nothing was sent. Write the full message (130-200 words) and run the command again")

    previous = already_sent(job_url)
    if previous and not force:
        raise EmailError(
            f"an email for this job was already sent on {previous.get('sent_at', '?')} to "
            f"{', '.join(previous.get('to', []))}; not sending twice"
        )

    sender, password = credentials()
    try:
        sender_name = config.load_profile().get("personal", {}).get("full_name", "")
    except Exception:  # profile missing in tests
        sender_name = ""
    msg = build_message(sender, sender_name, recipients, subject, body, files, copies)

    record = {
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "job_url": job_url,
        "to": recipients,
        "cc": copies,
        "subject": msg["Subject"],
        "attachments": [p.name for p in files],
        "message_id": msg["Message-ID"],
        "from": sender,
    }
    if dry_run:
        record["dry_run"] = True
        return record

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=60) as smtp:
            smtp.login(sender, password)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailError(f"Gmail rejected the login: {exc.smtp_error!r}") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"SMTP failure: {exc}") from exc

    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record
