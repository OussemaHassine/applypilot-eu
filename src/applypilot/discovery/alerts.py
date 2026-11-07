"""Job-alert emails as a discovery source (local patch, 2026-11-05).

HelloWork, the main French job board, forbids crawling its search pages in robots.txt
and has no feed; the way it wants to be read is the job alerts it emails to a registered
user. This module turns those alert emails in Gmail into postings for the normal
enrich -> score -> tailor -> apply pipeline:

  1. find alert emails not read before (IMAP, same app password as `applypilot inbox`)
  2. follow each tracking link just far enough to learn the posting id (no page load)
  3. load every new posting page once and read title, company, place and contract
  4. store it (site "hellowork", strategy "email_alert"); the URL primary key makes
     re-runs free and the alert_emails table keeps each email parsed once.

Indeed alerts are not read: their links resolve to ad-click redirects without a job id,
and JobSpy already searches Indeed directly. LinkedIn digests repeat what JobSpy finds.
Adding another board = one entry in ALERT_SOURCES plus a page parser.
"""

from __future__ import annotations

import email
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import requests

from applypilot import config
from applypilot.database import get_connection, store_jobs
from applypilot.discovery.locfilter import location_ok

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
REQUEST_GAP = 0.7        # seconds between posting-page loads: a person clicking, not a crawler
REDIRECT_GAP = 0.25      # the tracking redirects are cheap for their server and tell us the job id
HTTP_TIMEOUT = 20

_TAG = re.compile(r"<[^>]+>")
_HREF = re.compile(r'<a[^>]+href="([^"]+)"', re.I)


def _text(fragment: str) -> str:
    return html.unescape(_TAG.sub("", fragment)).strip()


def parse_hellowork(page: str, url: str) -> dict:
    """Title, company, place and contract from a hellowork.com/fr-fr/emplois/<id>.html page."""
    def grab(pattern: str) -> str:
        m = re.search(pattern, page, re.S)
        return _text(m.group(1)) if m else ""

    # Two layouts: employer postings (<span data-cy="jobTitle"> + company link + facts list) and
    # agency / interim postings (plain <h1><span>title</span><span>company</span></h1>, no facts).
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    spans = [_text(x) for x in re.findall(r"<(?:span|a)\b[^>]*>(.*?)</(?:span|a)>", h1.group(1), re.S)] if h1 else []
    title = grab(r'data-cy="jobTitle"[^>]*>(.*?)</span>') or (spans[0] if spans else "")
    company = grab(r'<h1[^>]*>.*?<a href="/fr-fr/entreprises/[^"]*"[^>]*>(.*?)</a>') or (spans[1] if len(spans) > 1 else "")
    facts = [_text(x) for x in re.findall(r'<li class="typo-long-m-bold[^"]*"[^>]*>(.*?)</li>', page, re.S)]
    place = facts[0] if facts else ""
    contract = facts[1] if len(facts) > 1 else ""
    region = ""
    m = re.search(r'dataLayer\.push\((\{[^<]*?"idOffre"[^<]*?\})\);', page, re.S)
    if m:
        try:
            d = json.loads(m.group(1))
            region = str(d.get("Region") or "")
            contract = contract or str(d.get("contrat") or "")
            if not place:   # "paris-75000" -> "Paris (75000)"; else the département slug
                ville = str(d.get("Ville") or "")
                vm = re.match(r"(.+)-(\d{5})$", ville)
                place = f"{vm.group(1).replace('-', ' ').title()} ({vm.group(2)})" if vm else \
                    (ville or str(d.get("Departement") or "")).replace("-", " ").title()
        except ValueError:
            pass
    summary = grab(r'<meta property="og:description" content="([^"]*)"')
    if not place:   # last resort: "... Business Analyst Commercial Paris (75) - Recrutement ..." in og:title
        om = re.search(r"([A-ZÀ-Ý][\w'’ -]*?) \((\d{2,3})\)", grab(r'<meta property="og:title" content="([^"]*)"'))
        if om:
            place = f"{om.group(1).split()[-1]} ({om.group(2)})"
    location = ", ".join(x for x in (place or region.replace("-", " ").title(), "France") if x)
    description = "\n".join(l for l in (f"Company: {company}" if company else "",
                                        f"Contract: {contract}" if contract else "", summary) if l)
    return {"url": url, "title": title, "location": location, "salary": None, "description": description}


ALERT_SOURCES = [
    {
        "site": "hellowork",
        "gmail_query": "from:emails.hellowork.com",
        # links worth following inside the email; everything else (unsubscribe, profile) is ignored
        "link_re": re.compile(r"^https://(emails\.hellowork\.com/clic/|www\.hellowork\.com/fr-fr/emplois/\d+\.html)"),
        "job_re": re.compile(r"https://www\.hellowork\.com/fr-fr/emplois/(\d+)\.html"),
        "canonical": "https://www.hellowork.com/fr-fr/emplois/{id}.html",
        "parse": parse_hellowork,
    },
]


def _ensure_table(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS alert_emails (
        message_id TEXT PRIMARY KEY, site TEXT, subject TEXT, received_at TEXT,
        processed_at TEXT, links INTEGER, new_jobs INTEGER)""")
    conn.commit()


def _html_body(msg: email.message.Message) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", "ignore")
    return ""


def _job_id(src: dict, url: str | None) -> str | None:
    m = src["job_re"].search(url or "")
    return m.group(1) if m else None


class _Fetcher:
    """Polite HTTP: one request at a time with a gap, browser user agent."""

    def __init__(self) -> None:
        self.http = requests.Session()
        self.http.headers["User-Agent"] = UA
        self._last = 0.0

    def _pace(self, gap: float = REQUEST_GAP) -> None:
        wait = gap - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def redirect_target(self, url: str) -> str:
        """Where a tracking link points, without loading the destination page."""
        self._pace(REDIRECT_GAP)
        r = self.http.get(url, allow_redirects=False, timeout=HTTP_TIMEOUT)
        if 300 <= r.status_code < 400 and r.headers.get("Location"):
            return requests.compat.urljoin(url, r.headers["Location"])
        return r.url

    def page(self, url: str) -> str:
        self._pace()
        r = self.http.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.text


def ingest_alerts(days: int = 7, dry_run: bool = False) -> dict:
    """Read job-alert emails from the last `days` days and store the postings they announce."""
    stats = {"emails": 0, "links": 0, "new": 0, "existing": 0, "filtered": 0, "errors": 0}
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if not address or not password:
        log.info("Gmail is not configured; job-alert emails skipped")
        return stats
    from applypilot.inbox import _connect, _fetch_by_uid   # same session rules (timeouts, errors)

    conn = get_connection()
    _ensure_table(conn)
    done = {r[0] for r in conn.execute("SELECT message_id FROM alert_emails")}
    search_cfg = config.load_search_config() or {}
    accept = search_cfg.get("location_accept", [])
    reject = search_cfg.get("location_reject_non_remote", [])
    fetch = _Fetcher()
    imap = _connect(address, password, readonly=True)
    try:
        for src in ALERT_SOURCES:
            known = {r[0] for r in conn.execute("SELECT url FROM jobs WHERE site = ?", (src["site"],))}
            typ, data = imap.uid("search", None, "X-GM-RAW", f'"{src["gmail_query"]} newer_than:{int(days)}d"')
            uids = data[0].split() if typ == "OK" and data and data[0] else []
            bodies: dict[bytes, bytes] = {}
            for i in range(0, len(uids), 20):
                bodies.update(_fetch_by_uid(imap, uids[i:i + 20], "(BODY.PEEK[])"))
            for uid in uids:
                raw = bodies.get(uid)
                if raw is None:
                    continue
                msg = email.message_from_bytes(raw)
                mid = (msg.get("Message-ID") or f"imap-{uid.decode()}").strip()
                if mid in done:
                    continue
                subject = _text(str(email.header.make_header(email.header.decode_header(msg.get("Subject") or ""))))
                hrefs = list(dict.fromkeys(h.replace("&amp;", "&") for h in _HREF.findall(_html_body(msg))
                                           if src["link_re"].match(h.replace("&amp;", "&"))))
                jobs: list[dict] = []
                seen_ids: set[str] = set()
                for href in hrefs:
                    try:
                        jid = _job_id(src, href) or _job_id(src, fetch.redirect_target(href))
                    except requests.RequestException as exc:
                        log.warning("Alert link failed (%s): %s", exc.__class__.__name__, href[:80])
                        stats["errors"] += 1
                        continue
                    if not jid or jid in seen_ids:
                        continue
                    seen_ids.add(jid)
                    url = src["canonical"].format(id=jid)
                    if url in known:
                        stats["existing"] += 1
                        continue
                    try:
                        job = src["parse"](fetch.page(url), url)
                    except requests.RequestException as exc:
                        log.warning("Posting page failed (%s): %s", exc.__class__.__name__, url)
                        stats["errors"] += 1
                        continue
                    if not job.get("title"):
                        log.warning("Could not read a title from %s; skipped", url)
                        stats["errors"] += 1
                        continue
                    if not location_ok(job.get("location"), accept, reject, text=job.get("title") or ""):
                        stats["filtered"] += 1
                        known.add(url)
                        continue
                    jobs.append(job)
                    known.add(url)
                    log.info("[%s] %s -- %s", src["site"], job["title"][:70], job["location"])
                stats["links"] += len(hrefs)
                stats["emails"] += 1
                if dry_run:
                    stats["new"] += len(jobs)
                    continue
                new, existing = store_jobs(conn, jobs, site=src["site"], strategy="email_alert")
                stats["new"] += new
                stats["existing"] += existing
                conn.execute("INSERT OR REPLACE INTO alert_emails VALUES (?,?,?,?,?,?,?)",
                             (mid, src["site"], subject[:200], msg.get("Date", ""),
                              datetime.now(timezone.utc).isoformat(timespec="seconds"), len(hrefs), new))
                conn.commit()
                done.add(mid)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    log.info("Job-alert emails: %d read, %d links, %d new postings, %d already known, %d outside your locations, %d errors",
             stats["emails"], stats["links"], stats["new"], stats["existing"], stats["filtered"], stats["errors"])
    return stats
