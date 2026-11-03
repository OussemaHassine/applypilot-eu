"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("haiku", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: Optional[bool] = typer.Option(None, "--headless/--no-headless", help="Browser without a window. Default: follow HEADLESS in run_settings.env (live)."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {'from run_settings.env (live)' if headless is None else headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
    )


@app.command()
def inbox(
    days: int = typer.Option(3, "--days", "-d", help="How many days of mail to scan."),
    watch: bool = typer.Option(False, "--watch", help="Keep running and re-check every --interval minutes."),
    interval: int = typer.Option(10, "--interval", help="Minutes between checks in --watch mode."),
    no_label: bool = typer.Option(False, "--no-label", help="Do not add ApplyPilot/* labels in Gmail."),
    no_notify: bool = typer.Option(False, "--no-notify", help="Do not show macOS notifications."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Classify and print only: no labels, notifications or saving."),
    show: bool = typer.Option(False, "--show", help="Print all stored responses and exit (no Gmail access)."),
) -> None:
    """Read Gmail, find replies from companies, label them and notify you."""
    _bootstrap()
    import time
    from rich.table import Table
    from applypilot.inbox import check_inbox, write_digest, CATEGORIES

    if show:
        path = write_digest()
        console.print(open(path, encoding="utf-8").read())
        return

    def once() -> None:
        try:
            new = check_inbox(days=days, label=not no_label, notify_user=not no_notify, dry_run=dry_run)
        except Exception as exc:
            console.print(f"[red]Inbox check failed:[/red] {exc}")
            return
        stamp = time.strftime("%H:%M")
        if not new:
            console.print(f"[dim]{stamp} no new job emails[/dim]")
            return
        t = Table(title=f"{stamp} new job emails")
        for col in ("Category", "Company", "Summary", "To do"):
            t.add_column(col, overflow="fold")
        for r in new:
            t.add_row(CATEGORIES.get(r["category"], (r["category"],))[0] or r["category"],
                      r["company"] or r["sender"][:30], r["summary"], r["action"] or "")
        console.print(t)
        if not dry_run:
            console.print("[dim]All responses: ~/ApplyPilot/responses.md[/dim]")

    once()
    while watch:
        time.sleep(max(1, interval) * 60)
        once()


@app.command()
def ui(
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind. Use 0.0.0.0 only with APPLYPILOT_UI_PASSWORD set."),
    port: int = typer.Option(8765, "--port", "-p", help="Port."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the browser."),
) -> None:
    """Start the web UI: dashboard, runs, scheduler and settings."""
    _bootstrap()
    import os
    import webbrowser
    if host not in ("127.0.0.1", "localhost") and not os.environ.get("APPLYPILOT_UI_PASSWORD"):
        console.print("[red]Refusing to listen on a public interface without APPLYPILOT_UI_PASSWORD.[/red]")
        raise typer.Exit(code=1)
    from applypilot.web.server import serve
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}"
    console.print(f"[bold]ApplyPilot UI[/bold] on {url}  (Ctrl+C to stop; the scheduler runs while this is open)")
    if open_browser:
        import threading
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    serve(host=host, port=port)


@app.command()
def followups(
    limit: int = typer.Option(10, "--limit", "-l", help="Max follow-ups to handle."),
    headless: Optional[bool] = typer.Option(None, "--headless/--no-headless", help="Browser without a window. Default: follow HEADLESS in run_settings.env (live)."),
    model: str = typer.Option("sonnet", "--model", "-m", help="Claude model."),
) -> None:
    """Act on emails that need a step: verify links, finish incomplete applications, give consent."""
    _bootstrap()
    from applypilot.config import check_tier
    check_tier(3, "follow-ups")
    from applypilot.apply.followup import run_followups
    st = run_followups(limit=limit, headless=headless, model=model)
    console.print(f"Follow-ups: {st['done']} done, {st['needs_human']} need you, {st['failed']} failed"
                  + (" (paused: Claude unavailable)" if st["paused"] else "") + f" of {st['total']}.")


@app.command("cover-letter")
def cover_letter(
    url: str = typer.Option(..., "--url", help="The job's URL (jobs.url) to write the cover letter for."),
    dest: Optional[str] = typer.Option(None, "--dest", help="Also copy the PDF into this folder as <Name>_Cover_Letter.pdf."),
) -> None:
    """Write the cover letter for ONE job, on demand.

    The apply agent runs this only when the application form has a cover-letter field or upload,
    so letters are not produced in bulk. Prints COVER_LETTER_PDF=, COVER_LETTER_TXT= and the text.
    """
    _bootstrap()
    from applypilot.scoring.cover_letter import generate_for_job
    try:
        r = generate_for_job(url, validation_mode="strict", dest_dir=dest)
    except Exception as exc:
        typer.echo(f"COVER_LETTER_ERROR={exc}")
        raise typer.Exit(code=1)
    typer.echo(f"COVER_LETTER_PDF={r['pdf'] or 'N/A'}")
    typer.echo(f"COVER_LETTER_TXT={r['txt']}")
    typer.echo("COVER_LETTER_TEXT_BEGIN")
    typer.echo(r["text"])
    typer.echo("COVER_LETTER_TEXT_END")


@app.command("send-email")
def send_email(
    to: list[str] = typer.Option(..., "--to", help="Recipient address, exactly as shown in the posting (repeat for several, max 3)."),
    subject: str = typer.Option(..., "--subject", help="Subject line, in the posting's language."),
    body_file: str = typer.Option(..., "--body-file", help="Plain-text file holding the message body."),
    attach: list[str] = typer.Option([], "--attach", help="PDF to attach (repeat). The tailored CV goes here."),
    job_url: Optional[str] = typer.Option(None, "--job-url", help="The job's URL (jobs.url): one email per job, recorded in logs/sent_emails.jsonl."),
    cc: list[str] = typer.Option([], "--cc", help="Copy address (repeat), only when the posting names one."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Build and print the message, send nothing."),
    force: bool = typer.Option(False, "--force", help="Send even if an email for this job URL was already sent."),
) -> None:
    """Send ONE application email from the candidate's Gmail (postings with no form, "send your CV to ...").

    Used by the apply agent. Prints EMAIL_SENT ... on success, EMAIL_ERROR=... (exit 1) otherwise.
    """
    _bootstrap()
    from applypilot.mailer import EmailError, send_application_email

    try:
        body = Path(body_file).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(f"EMAIL_ERROR=cannot read body file: {exc}")
        raise typer.Exit(code=1)
    try:
        rec = send_application_email(to, subject, body, attach, job_url=job_url, cc=cc,
                                     dry_run=dry_run, force=force)
    except EmailError as exc:
        typer.echo(f"EMAIL_ERROR={exc}")
        raise typer.Exit(code=1)
    if dry_run:
        typer.echo(f"EMAIL_DRY_RUN to={','.join(rec['to'])} subject={rec['subject']!r} "
                   f"attachments={','.join(rec['attachments']) or 'none'}")
        typer.echo("--- body ---")
        typer.echo(body.strip())
        return
    typer.echo(f"EMAIL_SENT to={','.join(rec['to'])} message_id={rec['message_id']} "
               f"attachments={','.join(rec['attachments']) or 'none'}")


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        f"Set GEMINI_API_KEY in {ENV_PATH} (or run 'applypilot init')"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


if __name__ == "__main__":
    app()
