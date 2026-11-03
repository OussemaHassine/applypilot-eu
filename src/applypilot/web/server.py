"""ApplyPilot web UI: dashboard, run control, scheduler and settings editor.

Start with `applypilot ui` (default http://127.0.0.1:8765). Everything reads and
writes the same files the CLI uses (APPLYPILOT_DIR). Set APPLYPILOT_UI_PASSWORD
to require HTTP basic auth (do this before exposing the port beyond localhost).
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from applypilot import config

APP_DIR: Path = config.APP_DIR
DB_PATH: Path = config.DB_PATH
LOG_DIR: Path = config.LOG_DIR
STATIC = Path(__file__).parent / "static"
UI_SETTINGS = APP_DIR / "ui_settings.json"
RUN_SETTINGS = APP_DIR / "run_settings.env"
KEYS_FILE = config.ENV_PATH
BACKUPS = APP_DIR / "backups"
RUN_DIR = LOG_DIR / "ui-runs"
LANGS = ("en", "fr", "it")

DEFAULT_UI = {
    "schedule": {"enabled": False, "time": "00:00", "days": [0, 1, 2, 3, 4, 5, 6], "auto_apply": True, "auto_pipeline": True},
    "inbox": {"enabled": False, "interval_min": 10, "days": 3},
    "prices": {  # USD per 1M tokens
        "llm_input": 0.10, "llm_cached_input": 0.01, "llm_output": 0.50,
        "apply_estimate_per_success": 0.50, "apply_estimate_per_failure": 0.20,
    },
    "last_round_date": None,
}
RUN_DEFAULTS = {"MIN_SCORE": "7", "MAX_APPLY": "0", "WORKERS": "2", "APPLY_MODEL": "sonnet", "HEADLESS": "0"}
SECRET_KEYS = ("OPENAI_API_KEY", "GEMINI_API_KEY", "CAPSOLVER_API_KEY", "GMAIL_APP_PASSWORD", "LLM_API_KEY")
KNOWN_KEYS = ("OPENAI_API_KEY", "GEMINI_API_KEY", "LLM_MODEL", "LLM_URL", "CAPSOLVER_API_KEY",
              "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")

COUNTRIES = [
    "Italy", "France", "Germany", "Netherlands", "Spain", "Portugal", "Belgium", "Ireland", "Switzerland",
    "Austria", "Poland", "Sweden", "Denmark", "United Kingdom", "Norway", "Finland", "Czech Republic",
    "Czechia", "Hungary", "Romania", "Greece", "Luxembourg", "Bulgaria", "Croatia", "Slovakia", "Slovenia",
    "Serbia", "Estonia", "Latvia", "Lithuania", "Malta", "Cyprus", "United Arab Emirates", "Qatar",
    "Saudi Arabia", "Tunisia",
]
COUNTRY_ALIASES = {
    "uk": "United Kingdom", "england": "United Kingdom", "scotland": "United Kingdom", "wales": "United Kingdom",
    "london": "United Kingdom", "italia": "Italy", "milan": "Italy", "milano": "Italy", "rome": "Italy",
    "roma": "Italy", "paris": "France", "lisbon": "Portugal", "lisboa": "Portugal", "porto": "Portugal",
    "madrid": "Spain", "barcelona": "Spain", "berlin": "Germany", "munich": "Germany", "münchen": "Germany",
    "amsterdam": "Netherlands", "brussels": "Belgium", "bruxelles": "Belgium", "dublin": "Ireland",
    "warsaw": "Poland", "kraków": "Poland", "krakow": "Poland", "bucharest": "Romania", "bucurești": "Romania",
    "prague": "Czech Republic", "budapest": "Hungary", "vienna": "Austria", "wien": "Austria",
    "zurich": "Switzerland", "zürich": "Switzerland", "dubai": "United Arab Emirates", "uae": "United Arab Emirates",
    "doha": "Qatar", "riyadh": "Saudi Arabia", "athens": "Greece", "stockholm": "Sweden", "copenhagen": "Denmark",
}


# ── helpers ──────────────────────────────────────────────────────────────

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def rows(sql: str, params: tuple = ()) -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def scalar(sql: str, params: tuple = ()):
    with db() as conn:
        r = conn.execute(sql, params).fetchone()
        return r[0] if r else None


def table_exists(name: str) -> bool:
    return bool(scalar("SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def country_of(location: str | None) -> str:
    loc = (location or "").lower()
    for c in COUNTRIES:
        if c.lower() in loc:
            return "Czech Republic" if c == "Czechia" else c
    for k, v in COUNTRY_ALIASES.items():
        if re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", loc):
            return v
    return "Remote / other"


def load_ui() -> dict:
    data = json.loads(json.dumps(DEFAULT_UI))
    if UI_SETTINGS.exists():
        try:
            saved = json.loads(UI_SETTINGS.read_text())
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(data.get(k), dict):
                    data[k].update(v)
                else:
                    data[k] = v
        except Exception:
            pass
    return data


def save_ui(data: dict) -> None:
    UI_SETTINGS.write_text(json.dumps(data, indent=2))


def backup(path: Path) -> None:
    if path.exists():
        BACKUPS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, BACKUPS / f"{path.name}.{stamp}")


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*(#.*)?$", line)
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def write_env_values(path: Path, values: dict[str, str]) -> None:
    """Update KEY=value lines in place (uncommenting if needed), append missing keys."""
    lines = path.read_text().splitlines() if path.exists() else []
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*#?\s*([A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in values and m.group(1) not in done:
            comment = ""
            cm = re.search(r"\s(#.*)$", line.split("=", 1)[1])
            if cm and not line.lstrip().startswith("#"):
                comment = "   " + cm.group(1)
            lines[i] = f"{m.group(1)}={values[m.group(1)]}{comment}"
            done.add(m.group(1))
    for k, v in values.items():
        if k not in done:
            lines.append(f"{k}={v}")
    backup(path)
    path.write_text("\n".join(lines) + "\n")


def mask(v: str) -> str:
    if not v:
        return ""
    return v[:4] + "…" + v[-2:] if len(v) > 8 else "••••"


def child_env() -> dict:
    env = os.environ.copy()
    env["APPLYPILOT_DIR"] = str(APP_DIR)
    home = Path.home()
    extra = [str(home / ".local/bin"), "/Library/TeX/texbin", "/opt/homebrew/bin", "/usr/local/bin"]
    node = shutil.which("node")
    if node:
        extra.insert(0, str(Path(node).parent))
    env["PATH"] = ":".join(extra + [env.get("PATH", "")])
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    return env


def applypilot_cmd() -> list[str]:
    exe = shutil.which("applypilot", path=child_env()["PATH"])
    return [exe] if exe else [sys.executable, "-m", "applypilot.cli"]


def external_processes() -> list[str]:
    """applypilot processes not started by this UI (CLI runs, other sessions)."""
    try:
        out = subprocess.run(["pgrep", "-fl", r"python.*applypilot (apply|run)"], capture_output=True, text=True).stdout
    except Exception:
        return []
    mine = {r.proc.pid for r in RUNS.values() if r.proc and r.proc.poll() is None}
    procs = []
    for line in out.splitlines():
        pid, _, cmd = line.partition(" ")
        if pid.isdigit() and int(pid) not in mine and not any(_is_child(int(pid), p) for p in mine):
            procs.append(re.sub(r"^.*?(applypilot .*)$", r"\1", cmd)[:120])
    return procs


def _is_child(pid: int, parent: int) -> bool:
    try:
        ppid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip() or 0)
    except Exception:
        return False
    seen = 0
    while ppid > 1 and seen < 10:
        if ppid == parent:
            return True
        try:
            ppid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(ppid)], capture_output=True, text=True).stdout.strip() or 0)
        except Exception:
            return False
        seen += 1
    return False


# ── runs ─────────────────────────────────────────────────────────────────

class Run:
    def __init__(self, kind: str, label: str, cmd: list[str], trigger: str):
        self.id = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        self.kind, self.label, self.cmd, self.trigger = kind, label, cmd, trigger
        self.started = datetime.now()
        self.ended: datetime | None = None
        self.rc: int | None = None
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        self.log = RUN_DIR / f"{self.id}-{kind}.log"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        fh = open(self.log, "w", encoding="utf-8")
        fh.write(f"$ {' '.join(self.cmd)}\n")
        fh.flush()
        env = child_env()
        env["APPLYPILOT_UI_RUN"] = "1"   # daily_run.sh then mirrors its output to stdout for the live view
        self.proc = subprocess.Popen(self.cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     cwd=str(APP_DIR), env=env, start_new_session=True)
        threading.Thread(target=self._wait, args=(fh,), daemon=True).start()

    def _wait(self, fh) -> None:
        self.rc = self.proc.wait()
        self.ended = datetime.now()
        fh.close()
        _save_history(self)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if not self.running:
            return
        try:
            pg = os.getpgid(self.proc.pid)
            os.killpg(pg, signal.SIGINT)
            time.sleep(2)
            if self.running:
                os.killpg(pg, signal.SIGINT)
            for _ in range(20):
                if not self.running:
                    break
                time.sleep(0.5)
            if self.running:
                os.killpg(pg, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def info(self, tail: int = 0) -> dict:
        d = {"id": self.id, "kind": self.kind, "label": self.label, "trigger": self.trigger,
             "started": self.started.isoformat(timespec="seconds"),
             "ended": self.ended.isoformat(timespec="seconds") if self.ended else None,
             "running": self.running, "rc": self.rc}
        if tail:
            d["log"] = tail_file(self.log, tail)
            if self.kind == "round" and len(d["log"].splitlines()) < 5:
                # older rounds wrote only to the nightly log; show that instead
                d["log"] += "\n" + tail_file(LOG_DIR / f"nightly-{self.started.date().isoformat()}.log", tail)
        return d


RUNS: dict[str, Run] = {}
RUNS_LOCK = threading.Lock()
HISTORY = RUN_DIR / "history.jsonl"
EXCLUSIVE = {"round", "discover", "pipeline", "apply", "apply-dry", "followups"}


def _save_history(run: Run) -> None:
    try:
        with open(HISTORY, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(run.info()) + "\n")
    except Exception:
        pass


def tail_file(path: Path, n: int) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 64000))
            data = fh.read().decode("utf-8", errors="replace")
        lines = [l for l in data.splitlines() if "HTTP Request:" not in l]
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def run_settings() -> dict[str, str]:
    vals = dict(RUN_DEFAULTS)
    vals.update({k: v for k, v in read_env(RUN_SETTINGS).items() if k in RUN_DEFAULTS})
    return vals


def build_cmd(kind: str) -> tuple[list[str], str]:
    s = run_settings()
    ap = applypilot_cmd()
    daily = APP_DIR / "daily_run.sh"
    if kind == "round":
        if daily.exists():
            return ["/bin/zsh" if Path("/bin/zsh").exists() else "/bin/bash", str(daily)], "Full round"
        kind = "pipeline"
    table = {
        "discover": (ap + ["run", "discover"], "Discover jobs"),
        # cover letters are written on demand by the apply agent (only when a form has a field for one)
        "pipeline": (ap + ["run", "enrich", "score", "tailor", "pdf", "-w", "2", "--min-score",
                           s["MIN_SCORE"], "--validation", "strict"], "Score & tailor"),
        "apply": (ap + ["apply", "--limit", s["MAX_APPLY"], "--min-score", s["MIN_SCORE"],
                        "--workers", s["WORKERS"], "--model", s["APPLY_MODEL"]], "Apply"),
        "apply-dry": (ap + ["apply", "--dry-run", "--limit", "1", "--model", s["APPLY_MODEL"]], "Dry run (1 job)"),
        "inbox": (ap + ["inbox", "--days", str(load_ui()["inbox"].get("days", 3))], "Check Gmail"),
        "followups": (ap + ["followups", "--limit", "10", "--model", s["APPLY_MODEL"]], "Email follow-ups"),
    }
    if kind not in table:
        raise HTTPException(400, f"unknown run kind {kind}")
    return table[kind]  # headless is read live from run_settings.env by the apply/followup code


# Which runs cannot overlap (2026-11-03). A kind only blocks on runs that use the same
# resource: applying uses the worker Chrome profiles (ports 9222+), follow-ups their own
# profile (worker 9, port 9231), enrich/score/tailor the OpenAI pipeline, discovery the
# boards. Gmail checks never block anything. A full round uses all of them.
CONFLICTS = {
    "apply":     {"apply", "apply-dry", "round"},
    "apply-dry": {"apply", "apply-dry", "round"},
    "followups": {"followups", "round"},
    "pipeline":  {"pipeline", "round"},
    "discover":  {"discover", "round"},
    "round":     set(EXCLUSIVE),
}
# ... and which processes started outside the UI (CLI, scripts, launchd) block each kind
_EXTERNAL_BLOCKERS = {
    "apply": r"applypilot apply", "apply-dry": r"applypilot apply", "followups": r"applypilot followups",
    "pipeline": r"applypilot run (?!discover)", "discover": r"applypilot run discover", "round": r".",
}


def start_run(kind: str, trigger: str = "manual") -> Run:
    with RUNS_LOCK:
        if kind in EXCLUSIVE:
            blockers = CONFLICTS[kind]
            busy = [r for r in RUNS.values() if r.running and r.kind in blockers]
            if busy:
                raise HTTPException(409, f"'{busy[0].label}' is already running")
            ext = [e for e in external_processes() if re.search(_EXTERNAL_BLOCKERS[kind], e)]
            if ext:
                raise HTTPException(409, "An ApplyPilot process started outside the UI is running: " + ext[0][:80])
        if kind == "inbox" and any(r.running and r.kind == "inbox" for r in RUNS.values()):
            raise HTTPException(409, "Inbox check already running")
        cmd, label = build_cmd(kind)
        run = Run(kind, label, cmd, trigger)
        run.start()
        RUNS[run.id] = run
        return run


# ── scheduler ────────────────────────────────────────────────────────────

SCHED_STATE = {"last_inbox": None, "next_round": None, "next_inbox": None, "events": []}


def _sched_event(msg: str) -> None:
    SCHED_STATE["events"] = ([{"at": datetime.now().isoformat(timespec="seconds"), "msg": msg}]
                             + SCHED_STATE["events"])[:30]


def next_round_time(ui: dict, now: datetime | None = None) -> datetime | None:
    sch = ui["schedule"]
    if not sch.get("enabled") or not sch.get("days"):
        return None
    now = now or datetime.now()
    hh, mm = (int(x) for x in sch.get("time", "00:00").split(":"))
    for add in range(0, 8):
        day = now + timedelta(days=add)
        cand = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand.weekday() in sch["days"] and cand > now - timedelta(minutes=1):
            if add == 0 and ui.get("last_round_date") == cand.date().isoformat():
                continue
            return cand
    return None


def _ready_to_apply() -> int:
    """Jobs the apply launcher would pick up right now (same rule as apply/launcher.py)."""
    s = run_settings()
    return scalar("SELECT count(*) FROM jobs WHERE fit_score >= ? AND tailored_resume_path IS NOT NULL "
                  "AND (apply_status IS NULL OR apply_status = 'failed') AND COALESCE(apply_attempts, 0) < ?",
                  (int(s["MIN_SCORE"]), config.DEFAULTS["max_apply_attempts"])) or 0


def _auto_pipeline(ui: dict, now: datetime) -> None:
    """Start a Score & tailor run whenever postings wait to be enriched, scored or tailored, so new
    discoveries, and postings a board throttled earlier (left pending), are handled without waiting
    for the nightly round."""
    if not ui["schedule"].get("auto_pipeline", True):
        return
    if any(r.running and r.kind in CONFLICTS["pipeline"] for r in RUNS.values()):
        return
    from applypilot.pipeline import _count_pending
    try:
        min_score = int(run_settings()["MIN_SCORE"])
        # compared stage by stage, not as a sum: a posting moving from "to enrich" to "to score"
        # keeps the total unchanged, which would look like a stuck backlog
        counts = tuple(_count_pending(st, min_score) for st in ("enrich", "score", "tailor", "pdf"))
    except Exception:
        return
    waiting = sum(counts)
    if not waiting:
        return
    last = SCHED_STATE.get("auto_pipeline") or {}
    # the same backlog after a run means those postings cannot be processed right now (a board
    # answering HTTP 429, say): try again in an hour rather than every few minutes
    wait_min = 60 if last.get("counts") == counts else 3
    if last.get("at") and (now - last["at"]).total_seconds() < wait_min * 60:
        return
    try:
        start_run("pipeline", trigger="auto")
        SCHED_STATE["auto_pipeline"] = {"at": now, "counts": counts}
        _sched_event(f"Auto score & tailor started: {waiting} posting(s) waiting")
    except HTTPException as exc:
        if exc.detail != last.get("msg"):
            _sched_event(f"Auto score & tailor waiting: {exc.detail}")
        SCHED_STATE["auto_pipeline"] = {"at": now, "counts": counts, "msg": exc.detail}


def _auto_apply(ui: dict, now: datetime) -> None:
    """Start an Apply run whenever tailored jobs are waiting, so applying never waits for the
    nightly round (user, 2026-11-03). Replaces the launchd continuous apply, which the UI could
    neither see nor control. MAX_APPLY=0 makes each run take everything ready, then stop."""
    if not ui["schedule"].get("auto_apply", True):
        return
    if any(r.running and r.kind in CONFLICTS["apply"] for r in RUNS.values()):
        return
    ready = _ready_to_apply()
    if not ready:
        return
    last = SCHED_STATE.get("auto_apply") or {}
    # an Apply run that ended with the same jobs still waiting means the launcher cannot take
    # them right now (usage limit, browser trouble, ...): retry every 20 minutes, not every tick
    wait_min = 20 if last.get("ready") == ready else 3
    if last.get("at") and (now - last["at"]).total_seconds() < wait_min * 60:
        return
    try:
        start_run("apply", trigger="auto")
        SCHED_STATE["auto_apply"] = {"at": now, "ready": ready}
        _sched_event(f"Auto-apply started: {ready} job(s) ready")
    except HTTPException as exc:
        if exc.detail != last.get("msg"):
            _sched_event(f"Auto-apply waiting: {exc.detail}")
        SCHED_STATE["auto_apply"] = {"at": now, "ready": ready, "msg": exc.detail}


def scheduler_loop() -> None:
    while True:
        try:
            ui = load_ui()
            now = datetime.now()
            nxt = next_round_time(ui, now)
            SCHED_STATE["next_round"] = nxt.isoformat(timespec="minutes") if nxt else None
            if nxt and nxt <= now:
                try:
                    start_run("round", trigger="schedule")
                    _sched_event("Scheduled round started")
                except HTTPException as exc:
                    _sched_event(f"Scheduled round skipped: {exc.detail}")
                ui["last_round_date"] = now.date().isoformat()
                save_ui(ui)
            _auto_pipeline(ui, now)
            _auto_apply(ui, now)
            ib = ui["inbox"]
            if ib.get("enabled"):
                last = SCHED_STATE["last_inbox"]
                interval = max(2, int(ib.get("interval_min", 10)))
                due = last is None or (now - last).total_seconds() >= interval * 60
                SCHED_STATE["next_inbox"] = (now if due else last + timedelta(minutes=interval)).isoformat(timespec="minutes")
                if due:
                    SCHED_STATE["last_inbox"] = now
                    try:
                        start_run("inbox", trigger="schedule")
                    except HTTPException:
                        pass
            else:
                SCHED_STATE["next_inbox"] = None
            if ib.get("enabled") and table_exists("email_responses"):
                cols = {r["name"] for r in rows("PRAGMA table_info(email_responses)")}
                pending = scalar("SELECT count(*) FROM email_responses WHERE followup_status='pending'") if "followup_status" in cols else 0
                # follow-ups use their own browser profile, so only another follow-up run, a full
                # round or a Gmail check (which writes the same rows) holds them back
                busy = any(r.running and r.kind in ("followups", "round", "inbox") for r in RUNS.values())
                if pending and not busy:
                    try:
                        start_run("followups", trigger="schedule")
                        _sched_event(f"Handling {pending} email follow-up(s)")
                    except HTTPException:
                        pass
        except Exception as exc:  # never let the scheduler die
            _sched_event(f"Scheduler error: {exc}")
        time.sleep(20)


# ── app ──────────────────────────────────────────────────────────────────

app = FastAPI(title="ApplyPilot", docs_url=None, redoc_url=None)
PASSWORD = os.environ.get("APPLYPILOT_UI_PASSWORD", "")


@app.middleware("http")
async def auth(request: Request, call_next):
    if PASSWORD:
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            try:
                _, pw = base64.b64decode(hdr[6:]).decode().split(":", 1)
                ok = secrets.compare_digest(pw, PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="ApplyPilot"'})
    return await call_next(request)


@app.on_event("startup")
def _startup() -> None:
    config.load_env()
    try:
        if table_exists("email_responses"):
            cols = {r["name"] for r in rows("PRAGMA table_info(email_responses)")}
            if "handled" not in cols:
                with db() as conn:
                    conn.execute("ALTER TABLE email_responses ADD COLUMN handled INTEGER DEFAULT 0")
    except Exception:
        pass
    threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()


# overview ---------------------------------------------------------------

@app.get("/api/overview")
def overview():
    now = datetime.now(timezone.utc)
    j = rows("""SELECT count(*) total, coalesce(sum(full_description IS NOT NULL),0) described,
                coalesce(sum(fit_score IS NOT NULL),0) scored, coalesce(sum(fit_score >= 7),0) matches,
                coalesce(sum(tailored_resume_path IS NOT NULL),0) tailored,
                coalesce(sum(apply_status='applied'),0) applied, coalesce(sum(apply_status='failed'),0) failed,
                coalesce(sum(apply_status='in_progress'),0) in_progress,
                coalesce(sum(tailored_resume_path IS NOT NULL AND apply_status IS NULL AND fit_score >= ?),0) queued
                FROM jobs""", (int(run_settings()["MIN_SCORE"]),))[0]
    day_ago = (now - timedelta(days=1)).isoformat()
    j["applied_24h"] = scalar("SELECT count(*) FROM jobs WHERE apply_status='applied' AND applied_at > ?", (day_ago,))
    j["parked"] = scalar("SELECT count(*) FROM jobs WHERE apply_status='failed' AND apply_error LIKE 'missing_required%'")

    # applications per day, last 14 days (local dates)
    per_day = Counter()
    for r in rows("SELECT applied_at FROM jobs WHERE apply_status='applied' AND applied_at IS NOT NULL"):
        try:
            per_day[datetime.fromisoformat(r["applied_at"]).astimezone().date().isoformat()] += 1
        except Exception:
            pass
    today = datetime.now().date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]

    countries = Counter(country_of(r["location"]) for r in rows("SELECT location FROM jobs WHERE apply_status='applied'"))
    fails = rows("""SELECT CASE WHEN apply_error LIKE 'missing_required%' THEN 'missing info'
                    ELSE apply_error END reason, count(*) n FROM jobs WHERE apply_status='failed'
                    GROUP BY reason ORDER BY n DESC""")

    resp = {"total": 0, "by": {}}
    if table_exists("email_responses"):
        by = dict((r["category"], r["n"]) for r in rows(
            "SELECT category, count(*) n FROM email_responses WHERE job_related=1 GROUP BY category"))
        resp = {"by": by,
                "replied_companies": scalar("""SELECT count(DISTINCT lower(company)) FROM email_responses WHERE job_related=1
                    AND category NOT IN ('confirmation','job_alert','other') AND company != ''""") or 0}

    # activity feed
    events = []
    for r in rows("""SELECT title, location, apply_status, apply_error, coalesce(applied_at, last_attempted_at) at, url
                     FROM jobs WHERE apply_status IN ('applied','failed') AND coalesce(applied_at, last_attempted_at) IS NOT NULL
                     ORDER BY at DESC LIMIT 25"""):
        events.append({"at": r["at"], "type": r["apply_status"], "title": r["title"], "sub": r["location"],
                       "detail": r["apply_error"] or "", "url": r["url"]})
    if table_exists("email_responses"):
        for r in rows("""SELECT received_at at, category, company, summary FROM email_responses WHERE job_related=1
                         AND category NOT IN ('confirmation','job_alert','other') ORDER BY received_at DESC LIMIT 25"""):
            events.append({"at": r["at"], "type": "email:" + (r["category"] or ""), "title": r["company"] or "",
                           "sub": r["summary"] or "", "detail": "", "url": None})
    events.sort(key=lambda e: e["at"] or "", reverse=True)

    todo = {"emails": [], "forms": [], "auto": []}
    if table_exists("email_responses"):
        cols = {r["name"] for r in rows("PRAGMA table_info(email_responses)")}
        has_fu = "followup" in cols
        fu_sel = ", followup, followup_status, followup_note" if has_fu else ", NULL followup, NULL followup_status, NULL followup_note"
        # for the human: action-needed emails that are not automatable (or where the assistant gave up)
        todo["emails"] = rows(f"""SELECT message_id, received_at, company, summary, action, subject{fu_sel} FROM email_responses
            WHERE job_related=1 AND coalesce(handled,0)=0 AND (
                  (category='info_request' AND coalesce(followup,'human') NOT IN ('verify_link','complete_application','consent','optional'))
               OR coalesce(followup_status,'') IN ('needs_human','failed'))
            ORDER BY received_at DESC LIMIT 12""")
        for e in todo["emails"]:
            e["gmail"] = f"https://mail.google.com/mail/u/0/#search/rfc822msgid%3A{(e['message_id'] or '').strip('<>')}"
        if has_fu:
            todo["auto"] = rows("""SELECT message_id, received_at, company, summary, followup, followup_status, followup_note
                FROM email_responses WHERE followup IN ('verify_link','complete_application','consent')
                AND coalesce(followup_status,'') IN ('pending','done') AND received_at > datetime('now','-7 days')
                ORDER BY (followup_status='pending') DESC, received_at DESC LIMIT 15""")
    todo["forms"] = rows("""SELECT url, title, location, replace(apply_error,'missing_required_','') missing FROM jobs
        WHERE apply_status='failed' AND apply_error LIKE 'missing_required%' ORDER BY last_attempted_at DESC LIMIT 20""")

    return {
        "counts": j,
        "todo": todo,
        "per_day": [{"day": d, "n": per_day.get(d, 0)} for d in days],
        "countries": [{"name": k, "n": v} for k, v in countries.most_common(12)],
        "failures": fails,
        "responses": resp,
        "activity": events[:30],
        "live": live_status(),
    }


def worker_tails(max_age: int = 180, lines: int = 8) -> list[dict]:
    """Last lines of the apply agents' notes: every worker that holds a job right now (even when its
    notes went quiet for a while), plus workers that wrote something in the last few minutes."""
    out = []
    now = time.time()
    active: dict[str, str] = {}
    try:
        with db() as conn:
            for agent, title in conn.execute(
                    "SELECT agent_id, title FROM jobs WHERE apply_status='in_progress' AND agent_id IS NOT NULL"):
                active[agent] = title or ""
    except Exception:
        pass
    for log in sorted(LOG_DIR.glob("worker-*.log")):
        try:
            age = int(now - log.stat().st_mtime)
            if age > max_age and log.stem not in active:
                continue
            text = tail_file(log, 60)
        except Exception:
            continue
        keep = [l for l in text.splitlines() if l.strip() and not re.match(r"^\s*>> browser_(snapshot|take_screenshot|wait_for)\s*$", l)]
        out.append({"worker": log.stem, "lines": keep[-lines:], "age": age, "job": active.get(log.stem)})
    return out


def live_status() -> dict:
    running = [r.info(tail=40) for r in RUNS.values() if r.running]
    in_prog = rows("SELECT title, location, agent_id, last_attempted_at FROM jobs WHERE apply_status='in_progress'")
    return {"runs": running, "external": external_processes(), "in_progress": in_prog,
            "workers": worker_tails(), "next_round": SCHED_STATE["next_round"], "next_inbox": SCHED_STATE["next_inbox"]}


@app.get("/api/live")
def live():
    return live_status()


# applications -----------------------------------------------------------

@app.get("/api/applications")
def applications(status: str = "applied", q: str = "", country: str = "", limit: int = 200, offset: int = 0):
    where, params = [], []
    if status == "applied":
        where.append("apply_status='applied'")
    elif status == "failed":
        where.append("apply_status='failed'")
    elif status == "parked":
        where.append("apply_status='failed' AND apply_error LIKE 'missing_required%'")
    elif status == "queued":
        where.append("tailored_resume_path IS NOT NULL AND apply_status IS NULL AND fit_score >= ?")
        params.append(int(run_settings()["MIN_SCORE"]))
    elif status == "matches":
        where.append("fit_score >= 7")
    if q:
        where.append("(title LIKE ? OR location LIKE ? OR full_description LIKE ?)")
        params += [f"%{q}%"] * 3
    sql = ("SELECT url, title, location, site, fit_score, apply_status, apply_error, applied_at, last_attempted_at, "
           "tailored_resume_path IS NOT NULL has_cv, cover_letter_path IS NOT NULL has_cl FROM jobs"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY coalesce(applied_at, last_attempted_at, scored_at, discovered_at) DESC")
    items = rows(sql, tuple(params))
    for it in items:
        it["country"] = country_of(it["location"])
    if country:
        items = [i for i in items if i["country"] == country]
    return {"total": len(items), "items": items[offset:offset + limit]}


def worker_notes(job: dict) -> str:
    """The apply agent's notes for this job, from logs/worker-*.log."""
    targets = {u for u in (job.get("application_url"), job.get("url")) if u}
    best = ""
    for log in sorted(LOG_DIR.glob("worker-*.log")):
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for block in re.split(r"\n={20,}\n(?=\[\d{4}-)", text):
            head = block[:600]
            if any(f"URL: {t}" in head for t in targets):
                best = block  # keep the latest attempt
    lines = [l for l in best.splitlines() if not re.match(r"^\s*>> browser_(snapshot|take_screenshot|wait_for)\s*$", l)]
    return "\n".join(lines).strip()[-8000:]


@app.get("/api/job")
def job_detail(url: str):
    r = rows("SELECT * FROM jobs WHERE url = ?", (url,))
    if not r:
        raise HTTPException(404, "job not found")
    job = r[0]

    def read(p):
        try:
            return Path(p).with_suffix(".txt").read_text(encoding="utf-8") if p else ""
        except Exception:
            return ""

    cv_pdf = Path(job["tailored_resume_path"]).with_suffix(".pdf") if job.get("tailored_resume_path") else None
    cl_pdf = Path(job["cover_letter_path"]).with_suffix(".pdf") if job.get("cover_letter_path") else None
    emails = rows("SELECT received_at, category, company, summary, action, subject FROM email_responses "
                  "WHERE job_url = ? ORDER BY received_at DESC", (url,)) if table_exists("email_responses") else []
    return {
        "title": job["title"], "location": job["location"], "country": country_of(job["location"]),
        "site": job["site"], "url": job["url"], "application_url": job.get("application_url"),
        "fit_score": job["fit_score"], "score_reasoning": job["score_reasoning"],
        "status": job["apply_status"], "error": job["apply_error"], "applied_at": job["applied_at"],
        "description": (job["full_description"] or "")[:6000],
        "cv_text": read(job.get("tailored_resume_path")),
        "cover_text": read(job.get("cover_letter_path")),
        "cv_pdf": f"/files/cv/{cv_pdf.name}" if cv_pdf and cv_pdf.exists() else None,
        "cl_pdf": f"/files/cl/{cl_pdf.name}" if cl_pdf and cl_pdf.exists() else None,
        "agent_notes": worker_notes(job),
        "emails": emails,
    }


@app.get("/files/{kind}/{name}")
def files(kind: str, name: str):
    base = {"cv": config.TAILORED_DIR, "cl": config.COVER_LETTER_DIR}.get(kind)
    if not base:
        raise HTTPException(404)
    path = (base / name).resolve()
    if base.resolve() not in path.parents or not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


# responses --------------------------------------------------------------

@app.get("/api/responses")
def responses(category: str = "", include_handled: int = 0):
    if not table_exists("email_responses"):
        return {"items": [], "counts": {}}
    where = "job_related=1"
    params: tuple = ()
    if category:
        where += " AND category = ?"
        params = (category,)
    if not include_handled:
        where += " AND coalesce(handled,0)=0"
    items = rows(f"SELECT * FROM email_responses WHERE {where} ORDER BY received_at DESC LIMIT 400", params)
    for it in items:
        mid = (it.get("message_id") or "").strip("<>")
        it["gmail"] = f"https://mail.google.com/mail/u/0/#search/rfc822msgid%3A{mid}"
    counts = dict((r["category"], r["n"]) for r in rows(
        "SELECT category, count(*) n FROM email_responses WHERE job_related=1 GROUP BY category"))
    return {"items": items, "counts": counts}


# runs & scheduler ---------------------------------------------------------

@app.get("/api/runs")
def runs_list():
    current = sorted((r.info(tail=200) for r in RUNS.values()), key=lambda d: d["started"], reverse=True)
    history = []
    if HISTORY.exists():
        for line in HISTORY.read_text().splitlines()[-40:]:
            try:
                history.append(json.loads(line))
            except Exception:
                pass
    ids = {r["id"] for r in current}
    history = [h for h in reversed(history) if h["id"] not in ids]
    return {"current": current, "history": history[:30], "external": external_processes(),
            "scheduler": {**SCHED_STATE, "last_inbox": SCHED_STATE["last_inbox"].isoformat(timespec="minutes")
                          if SCHED_STATE["last_inbox"] else None},
            "workers": worker_tails(),
            "nightly_log": tail_file(LOG_DIR / f"nightly-{datetime.now().date().isoformat()}.log", 250)}


@app.post("/api/runs/{kind}")
def runs_start(kind: str):
    return start_run(kind).info()


@app.post("/api/runs/{run_id}/stop")
def runs_stop(run_id: str):
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(404)
    threading.Thread(target=run.stop, daemon=True).start()
    return {"stopping": run_id}


@app.get("/api/runs/{run_id}/log")
def runs_log(run_id: str, lines: int = 300):
    run = RUNS.get(run_id)
    path = run.log if run else next(iter(RUN_DIR.glob(f"{run_id}-*.log")), None)
    if not path:
        raise HTTPException(404)
    return {"log": tail_file(path, lines)}


@app.get("/api/schedule")
def schedule_get():
    ui = load_ui()
    return {"schedule": ui["schedule"], "inbox": ui["inbox"], "next_round": SCHED_STATE["next_round"],
            "next_inbox": SCHED_STATE["next_inbox"], "events": SCHED_STATE["events"]}


@app.put("/api/schedule")
def schedule_put(data: dict = Body(...)):
    ui = load_ui()
    sch, ib = data.get("schedule", {}), data.get("inbox", {})
    if "time" in sch and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", sch["time"]):
        raise HTTPException(400, "time must be HH:MM")
    ui["schedule"].update({k: v for k, v in sch.items() if k in ("enabled", "time", "days", "auto_apply", "auto_pipeline")})
    ui["inbox"].update({k: v for k, v in ib.items() if k in ("enabled", "interval_min", "days")})
    save_ui(ui)
    nxt = next_round_time(ui)
    SCHED_STATE["next_round"] = nxt.isoformat(timespec="minutes") if nxt else None
    _sched_event("Schedule updated")
    return schedule_get()


# settings: profile / searches / resumes / run settings / keys ------------

@app.get("/api/profile")
def profile_get():
    data = json.loads(config.PROFILE_PATH.read_text())
    return data


@app.put("/api/profile")
def profile_put(data: dict = Body(...)):
    if not isinstance(data.get("personal"), dict) or not data["personal"].get("full_name"):
        raise HTTPException(400, "profile must contain personal.full_name")
    backup(config.PROFILE_PATH)
    config.PROFILE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return {"saved": True}


@app.get("/api/searches")
def searches_get():
    raw = config.SEARCH_CONFIG_PATH.read_text()
    return {"raw": raw, "parsed": yaml.safe_load(raw)}


@app.put("/api/searches")
def searches_put(data: dict = Body(...)):
    if "raw" in data:
        try:
            parsed = yaml.safe_load(data["raw"])
        except yaml.YAMLError as exc:
            raise HTTPException(400, f"YAML error: {exc}")
        text = data["raw"]
    else:
        parsed = data.get("parsed")
        text = ("# ApplyPilot search configuration (saved from the web UI).\n"
                + yaml.safe_dump(parsed, sort_keys=False, allow_unicode=True, width=120))
    if not isinstance(parsed, dict) or not parsed.get("queries") or not parsed.get("locations"):
        raise HTTPException(400, "searches need at least one query and one location")
    backup(config.SEARCH_CONFIG_PATH)
    config.SEARCH_CONFIG_PATH.write_text(text)
    return {"saved": True}


# watch / hide the worker browsers (local patch 2026-11-03: "switch between live vision and headless") ----

def _worker_chrome_pids() -> list[int]:
    try:
        out = subprocess.run(["pgrep", "-f", "remote-debugging-port=92"], capture_output=True, text=True).stdout.split()
        return [int(x) for x in out]
    except Exception:
        return []


def _set_worker_windows_visible(visible: bool) -> int:
    """Hide/show the windows of running worker browsers (macOS, best effort: the first call asks
    for permission to control System Events). Headless browsers have no window to show."""
    pids = _worker_chrome_pids()
    if not pids or os.uname().sysname != "Darwin":
        return 0
    ids = ", ".join(str(x) for x in pids)
    script = (f'tell application "System Events" to set visible of '
              f'(every process whose unix id is in {{{ids}}}) to {"true" if visible else "false"}')
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=8)
        return len(pids) if r.returncode == 0 else 0
    except Exception:
        return 0


@app.get("/api/watch")
def watch_get():
    return {"watch": not config.headless_setting()}


@app.post("/api/watch")
def watch_set(data: dict = Body(...)):
    """watch=true: browsers get a visible window from the next job on (and hidden ones reappear);
    watch=false: browsers run headless from the next job on, and open windows are hidden now."""
    watch = bool(data.get("watch"))
    write_env_values(RUN_SETTINGS, {"HEADLESS": "0" if watch else "1"})
    touched = _set_worker_windows_visible(watch)
    return {"watch": watch, "windows_touched": touched}


@app.get("/api/resumes")
def resumes_get():
    out = {}
    for lang in LANGS:
        p = APP_DIR / f"resume_{lang}.txt"
        out[lang] = p.read_text(encoding="utf-8") if p.exists() else ""
    return out


@app.put("/api/resumes/{lang}")
def resumes_put(lang: str, data: dict = Body(...)):
    if lang not in LANGS:
        raise HTTPException(404)
    text = data.get("text", "")
    if len(text.strip()) < 200:
        raise HTTPException(400, "CV text looks empty")
    p = APP_DIR / f"resume_{lang}.txt"
    backup(p)
    p.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    if lang == "en":
        (APP_DIR / "resume.txt").write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    return {"saved": True}


@app.get("/api/resumes/{lang}/preview")
def resumes_preview(lang: str):
    """Render the base CV of a language as a PDF (how the template looks with your real content)."""
    if lang not in LANGS:
        raise HTTPException(404)
    from applypilot.scoring.pdf import convert_to_pdf
    src = APP_DIR / f"resume_{lang}.txt"
    tmp = Path(tempfile.mkdtemp(prefix="ap_preview_"))
    txt = tmp / f"base_cv_{lang}.txt"
    # The base CVs use localized headers; map them to the internal English keys the renderer expects.
    mapping = {"PROFILO": "SUMMARY", "PROFIL": "SUMMARY", "PROFILE": "SUMMARY",
               "ESPERIENZA PROFESSIONALE": "EXPERIENCE", "EXPÉRIENCE PROFESSIONNELLE": "EXPERIENCE",
               "PROFESSIONAL EXPERIENCE": "EXPERIENCE", "FORMAZIONE": "EDUCATION", "FORMATION": "EDUCATION",
               "PROGETTI": "PROJECTS", "PROJETS": "PROJECTS", "COMPETENZE": "TECHNICAL SKILLS",
               "COMPÉTENCES": "TECHNICAL SKILLS", "SKILLS": "TECHNICAL SKILLS"}
    lines = [mapping.get(l.strip(), l) for l in src.read_text(encoding="utf-8").splitlines()]
    out, entry_lines = [], []
    for l in lines:  # "Company | Role | Place | Dates" headers -> title + subtitle
        if "|" in l and not l.startswith("-") and l.count("|") >= 2 and l.strip() and not re.search(r"@", l):
            parts = [p.strip() for p in l.split("|")]
            out += [f"{parts[1]} — {parts[0]}" if len(parts) > 1 else parts[0], " | ".join(parts[2:])]
        else:
            out.append(l)
    txt.write_text("\n".join(out), encoding="utf-8")
    pdf = convert_to_pdf(txt, lang=lang)
    return FileResponse(pdf, media_type="application/pdf")


@app.get("/api/settings")
def settings_get():
    env = read_env(KEYS_FILE)
    keys = {k: {"set": bool(env.get(k)), "value": mask(env.get(k, "")) if k in SECRET_KEYS else env.get(k, "")}
            for k in KNOWN_KEYS}
    ui = load_ui()
    return {"run": run_settings(), "keys": keys, "prices": ui["prices"], "app_dir": str(APP_DIR)}


@app.put("/api/settings")
def settings_put(data: dict = Body(...)):
    run = data.get("run") or {}
    if run:
        vals = {k: str(v).strip() for k, v in run.items() if k in RUN_DEFAULTS}
        if "MIN_SCORE" in vals and not (vals["MIN_SCORE"].isdigit() and 1 <= int(vals["MIN_SCORE"]) <= 10):
            raise HTTPException(400, "MIN_SCORE must be 1-10")
        for k in ("MAX_APPLY", "WORKERS"):
            if k in vals and not vals[k].isdigit():
                raise HTTPException(400, f"{k} must be a number")
        write_env_values(RUN_SETTINGS, vals)
    keys = {k: str(v).strip() for k, v in (data.get("keys") or {}).items() if k in KNOWN_KEYS and str(v).strip()}
    if keys:
        write_env_values(KEYS_FILE, keys)
        for k, v in keys.items():
            os.environ[k] = v
    if data.get("prices"):
        ui = load_ui()
        ui["prices"].update({k: float(v) for k, v in data["prices"].items() if k in ui["prices"]})
        save_ui(ui)
    return settings_get()


@app.get("/api/followups")
def followups_list():
    if not table_exists("email_responses"):
        return {"items": [], "pending": 0}
    cols = {r["name"] for r in rows("PRAGMA table_info(email_responses)")}
    if "followup" not in cols:
        return {"items": [], "pending": 0}
    items = rows("""SELECT message_id, received_at, company, subject, summary, followup, followup_link, followup_status, followup_note
                    FROM email_responses WHERE followup IN ('verify_link','complete_application','consent')
                    ORDER BY (followup_status='pending') DESC, received_at DESC LIMIT 100""")
    return {"items": items, "pending": sum(1 for i in items if i["followup_status"] == "pending")}


@app.post("/api/followups/{message_id:path}/reset")
def followups_reset(message_id: str):
    with db() as conn:
        conn.execute("UPDATE email_responses SET followup_status='pending', handled=0 WHERE message_id=?", (message_id,))
        conn.commit()
    return {"ok": True}


# actions on jobs / emails / external processes ------------------------------

@app.post("/api/job/action")
def job_action(data: dict = Body(...)):
    url, action = data.get("url"), data.get("action")
    if not url or not rows("SELECT 1 FROM jobs WHERE url=?", (url,)):
        raise HTTPException(404, "job not found")
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        if action == "skip":
            conn.execute("UPDATE jobs SET apply_status='failed', apply_error='skipped by you', apply_attempts=99, agent_id=NULL WHERE url=?", (url,))
        elif action == "requeue":
            conn.execute("UPDATE jobs SET apply_status=NULL, apply_error=NULL, apply_attempts=0, agent_id=NULL WHERE url=?", (url,))
        elif action == "mark_applied":
            conn.execute("UPDATE jobs SET apply_status='applied', applied_at=?, apply_error=NULL, agent_id=NULL WHERE url=?", (now, url))
        elif action == "retailor":
            conn.execute("""UPDATE jobs SET tailored_resume_path=NULL, tailored_at=NULL, tailor_attempts=0,
                            cover_letter_path=NULL, cover_letter_at=NULL, cover_attempts=0,
                            apply_status=CASE WHEN apply_status='applied' THEN apply_status ELSE NULL END,
                            apply_error=CASE WHEN apply_status='applied' THEN apply_error ELSE NULL END WHERE url=?""", (url,))
        else:
            raise HTTPException(400, "unknown action")
        conn.commit()
    return {"ok": True}


@app.post("/api/jobs/requeue_parked")
def requeue_parked():
    with db() as conn:
        cur = conn.execute("UPDATE jobs SET apply_status=NULL, apply_error=NULL, apply_attempts=0, agent_id=NULL "
                           "WHERE apply_status='failed' AND apply_error LIKE 'missing_required%'")
        conn.commit()
        return {"requeued": cur.rowcount}


@app.post("/api/responses/handled")
def responses_handled(data: dict = Body(...)):
    if not table_exists("email_responses"):
        raise HTTPException(404)
    with db() as conn:
        conn.execute("UPDATE email_responses SET handled=? WHERE message_id=?", (1 if data.get("handled") else 0, data.get("message_id")))
        conn.commit()
    return {"ok": True}


@app.get("/api/applications.csv")
def applications_csv(status: str = "applied"):
    import csv
    import io
    items = applications(status=status, limit=100000)["items"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "title", "country", "location", "board", "fit_score", "status", "detail", "url"])
    for it in items:
        w.writerow([(it["applied_at"] or it["last_attempted_at"] or "")[:16], it["title"], it["country"], it["location"],
                    it["site"], it["fit_score"], it["apply_status"] or "queued", it["apply_error"] or "", it["url"]])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="applypilot-{status}-{datetime.now().date()}.csv"'})


@app.post("/api/test/gmail")
def test_gmail():
    import imaplib
    import socket
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not address or not password:
        return {"ok": False, "error": "Gmail address or app password not set"}
    try:
        socket.setdefaulttimeout(12)
        im = imaplib.IMAP4_SSL("imap.gmail.com")
        im.login(address, password)
        typ, data = im.select("INBOX", readonly=True)
        im.logout()
        return {"ok": True, "detail": f"Signed in as {address}; {int(data[0]) if data and data[0] else '?'} messages in Inbox"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        socket.setdefaulttimeout(None)


@app.post("/api/test/llm")
def test_llm():
    try:
        from applypilot.llm import LLMClient, _detect_provider
        base, model, key = _detect_provider()
        client = LLMClient(base, model, key)
        t0 = time.time()
        reply = client.ask("Reply with exactly the word OK.", max_tokens=20)
        return {"ok": True, "detail": f"{model} answered in {time.time() - t0:.1f}s: {reply.strip()[:40]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


@app.post("/api/runs/external/stop")
def stop_external():
    """Politely stop ApplyPilot processes that were not started by this UI (CLI, n8n, scripts)."""
    stopped = 0
    for pattern in (r"stage\.sh|daily_run\.sh", r"python.*applypilot (apply|run)"):
        try:
            out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
        except Exception:
            out = []
        for pid in out:
            try:
                os.kill(int(pid), signal.SIGINT)
                stopped += 1
            except Exception:
                pass
    time.sleep(3)
    with db() as conn:
        conn.execute("UPDATE jobs SET apply_status=NULL, agent_id=NULL WHERE apply_status='in_progress'")
        conn.commit()
    return {"signalled": stopped, "still_running": external_processes()}


# costs --------------------------------------------------------------------

def _jsonl(path: Path) -> list[dict]:
    out = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


@app.get("/api/costs")
def costs():
    pr = load_ui()["prices"]
    llm = _jsonl(LOG_DIR / "llm_usage.jsonl")
    by_stage: dict[str, dict] = defaultdict(lambda: {"calls": 0, "input": 0, "cached": 0, "output": 0, "reasoning": 0, "usd": 0.0})
    by_day: dict[str, float] = defaultdict(float)
    for r in llm:
        s = by_stage[r.get("stage", "other")]
        fresh = max(0, r.get("input", 0) - r.get("cached_input", 0))
        usd = (fresh * pr["llm_input"] + r.get("cached_input", 0) * pr["llm_cached_input"]
               + r.get("output", 0) * pr["llm_output"]) / 1e6
        s["calls"] += 1
        s["input"] += r.get("input", 0)
        s["cached"] += r.get("cached_input", 0)
        s["output"] += r.get("output", 0)
        s["reasoning"] += r.get("reasoning", 0)
        s["usd"] += usd
        try:
            by_day[datetime.fromisoformat(r["ts"]).astimezone().date().isoformat()] += usd
        except Exception:
            pass
    apply = _jsonl(LOG_DIR / "apply_usage.jsonl")
    measured = sum(a.get("cost_usd", 0) or 0 for a in apply)
    applied = scalar("SELECT count(*) FROM jobs WHERE apply_status='applied'") or 0
    failed = scalar("SELECT count(*) FROM jobs WHERE apply_status='failed'") or 0
    estimate = applied * pr["apply_estimate_per_success"] + failed * pr["apply_estimate_per_failure"]
    return {
        "prices": pr,
        "llm": {"by_stage": by_stage, "by_day": [{"day": d, "usd": round(v, 4)} for d, v in sorted(by_day.items())],
                "total_usd": round(sum(s["usd"] for s in by_stage.values()), 4), "calls": len(llm)},
        "apply": {"measured_runs": len(apply), "measured_usd": round(measured, 2),
                  "avg_measured": round(measured / len(apply), 3) if apply else None,
                  "estimate_all_time_usd": round(estimate, 2), "applied": applied, "failed": failed,
                  "recent": apply[-20:][::-1]},
    }


@app.get("/api/doctor")
def doctor():
    env = child_env()
    checks = {
        "applypilot": bool(shutil.which("applypilot", path=env["PATH"])),
        "claude": bool(shutil.which("claude", path=env["PATH"])),
        "npx": bool(shutil.which("npx", path=env["PATH"])),
        "pdflatex": bool(shutil.which("pdflatex", path=env["PATH"])),
        "chrome": _has_chrome(),
        "llm_key": any(os.environ.get(k) for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "LLM_URL")),
        "gmail": bool(os.environ.get("GMAIL_APP_PASSWORD")),
    }
    return checks


def _has_chrome() -> bool:
    try:
        return bool(config.get_chrome_path())
    except Exception:
        return False


# static -------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")
