# ApplyPilot (EU edition)

An AI job-application pipeline that finds jobs across Europe, scores how well each one
fits your CV, writes a tailored one-page CV and cover letter in the posting's language,
fills in the application form in a real browser, and then watches your Gmail for replies.

This is a fork of [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot)
(AGPL-3.0), extended for European job hunting in English, French and Italian. The
original README is in [docs/UPSTREAM_README.md](docs/UPSTREAM_README.md).

> Applications go out in your name. Read the profile it builds, keep the facts true,
> and start with `applypilot apply --dry-run` (fills a form, never submits).

---

## What it does

```
discover ─► enrich ─► score ─► tailor ─► cover ─► pdf ─► apply ─► inbox
 job boards  full      AI 1-10   CV per    letter   LaTeX   browser   Gmail replies:
 + company   posting   fit vs    job,      per job  1-page  agent     label + notify
 portals     text      your CV   checked                    fills form
```

| Stage | What happens | Engine |
|---|---|---|
| **discover** | Searches Indeed, LinkedIn and Google Jobs for every title × market in `searches.yaml`, plus ~50 company Workday portals and direct career sites. Drops postings outside your accepted locations. | [JobSpy](https://github.com/Bunsly/JobSpy), HTTP |
| **enrich** | Downloads the full description and the real "apply" link of each posting (structured data first, AI extraction as a fallback). | Playwright + LLM |
| **score** | Rates each posting 1–10 against your base CV. Internships, PhDs, over-senior and off-domain roles are capped. | LLM |
| **tailor** | Rewrites your CV for each job scoring ≥ `MIN_SCORE`, in the posting's language, then checks it (see *Safety*). | LLM + validators + AI judge |
| **cover** | Writes a 3-paragraph cover letter in the posting's language, signed with your full name. | LLM |
| **pdf** | Renders the CV with LaTeX (shrinks to stay on one page) and falls back to HTML if LaTeX fails. | pdflatex / Chromium |
| **apply** | A Claude agent opens the posting in Chrome (your logged-in profile copy), fills every page, uploads CV and letter, answers screening questions from your profile and submits. Skips duplicates of roles already applied to. | Claude Code CLI + Playwright MCP |
| **inbox** | Reads recent Gmail, classifies job emails (confirmation, rejection, interview, assessment, offer, info request, recruiter), adds `ApplyPilot/…` labels, keeps a list in `responses.md`, and decides what an assistant can do about each one. Notifies you only for things a human must decide. | IMAP + LLM |
| **followups** | Acts on emails that ask for a step: clicks verification / activation links, signs in and finishes incomplete applications, gives data-processing consent. Interviews, assessments, offers and anything needing judgment are left to you. | Claude Code CLI + Playwright MCP |

### What this fork adds to upstream

- **Multilingual**: one base CV per language (EN/FR/IT); the posting's language picks the CV, cover letter and form answers; localized PDF headings.
- **Per-country identity**: a different home city per job country (e.g. one city for French jobs), salary bands per country and contract type, languages that only appear for some regions.
- **Anti-fabrication**: code checks reject any number or skill not in your base CV; zero-tolerance AI judge; no added seniority; every base-CV project kept; strict mode never ships a CV the judge flagged.
- **LaTeX CVs** with automatic one-page fitting and HTML fallback.
- **Smarter apply agent**: duplicate detection across boards/cities/languages, a strict "never guess facts" rule (parks the job as `missing_required_<fact>` instead), favourable answers to preference questions, Gmail-in-browser for verification codes, pauses (instead of failing jobs) when Claude hits a usage limit.
- **European search config**: 25 markets including Gulf countries, whole-word location filter that rejects US/Canada/APAC remote postings.
- **Gmail response tracker** (`applypilot inbox`) and **automatic email follow-ups** (`applypilot followups`): verification links, unfinished applications and consent requests are handled by the agent; only decisions reach you.
- **Usage logging**: every LLM call → `logs/llm_usage.jsonl`, every application → `logs/apply_usage.jsonl`.

---

## Requirements

| Need | Why | Install |
|---|---|---|
| macOS or Linux, Python ≥ 3.11 | runtime | — |
| Google Chrome | the apply agent drives a copy of your Chrome profile | — |
| Node.js ≥ 18 (`npx`) | Playwright MCP server for the agent | `brew install node` |
| [Claude Code](https://docs.claude.com/claude-code) CLI, logged in | the apply agent | `npm i -g @anthropic-ai/claude-code` |
| One LLM key: OpenAI **or** Gemini **or** a local OpenAI-compatible server | scoring, CVs, letters, email sorting | — |
| LaTeX (optional) | nicer CVs; without it HTML is used | `brew install --cask basictex` |
| Gmail app password (optional) | `applypilot inbox` | see below |

## Install

```bash
git clone https://github.com/<you>/applypilot-eu.git ~/ApplyPilot/source
cd ~/ApplyPilot/source
python3 -m venv .venv && source .venv/bin/activate      # or: uv venv
pip install -e .
pip install --no-deps python-jobspy
pip install pydantic tls-client requests markdownify regex pypdf curl_cffi beautifulsoup4
playwright install chromium

# a launcher that always points at your data folder
mkdir -p ~/.local/bin
cat > ~/.local/bin/applypilot <<'EOF'
#!/bin/sh
export APPLYPILOT_DIR="${APPLYPILOT_DIR:-$HOME/ApplyPilot}"
exec "$HOME/ApplyPilot/source/.venv/bin/applypilot" "$@"
EOF
chmod +x ~/.local/bin/applypilot
```

## Configure

All personal data lives **outside the repo** in `~/ApplyPilot/` (the `APPLYPILOT_DIR`). Every file
below can also be edited from the web app.
Start from the files in [`examples/`](examples/):

```bash
cp examples/profile.example.json   ~/ApplyPilot/profile.json
cp examples/searches.example.yaml  ~/ApplyPilot/searches.yaml
cp examples/api_keys.example.env   ~/ApplyPilot/api_keys.env      # add your key(s)
cp examples/run_settings.env       ~/ApplyPilot/run_settings.env
cp daily_run.sh                    ~/ApplyPilot/daily_run.sh
# base CVs (plain text, ALL-CAPS section headers), one per language you apply in:
#   ~/ApplyPilot/resume_en.txt  resume_fr.txt  resume_it.txt   (+ resume.txt = default)
applypilot doctor
```

| File | What you edit there |
|---|---|
| `profile.json` | Identity, contact, address, per-country location, work rights, notice period, salary rules, languages, extra facts (date of birth, licence…), allowed skills, numbers the CV may quote. **Anything not in this file is never invented**: forms that require it get parked. |
| `resume_<lang>.txt` | The source of truth for every tailored CV. Add a job, project or skill here and all future CVs include it. |
| `searches.yaml` | Job titles, markets, freshness, accepted/rejected locations, boards. |
| `run_settings.env` | `MIN_SCORE`, `MAX_APPLY`, `WORKERS`, `APPLY_MODEL`, `HEADLESS`. |
| `api_keys.env` | `OPENAI_API_KEY` + `LLM_MODEL` (or `GEMINI_API_KEY` / `LLM_URL`), Gmail address + app password. |

## Web app

```bash
applypilot ui            # http://127.0.0.1:8765, opens your browser
```

| Page | What it is for |
|---|---|
| **Overview** | Live dashboard: applications sent, replies, interviews, action needed, queue, applications per day, the funnel from posting to application, countries, why applications were not sent, recent activity, what is running right now. Refreshes every 10 s. |
| **Applications** | Every job by state (sent, ready, needs info, not sent, all matches) with search and country filter. A row opens the tailored CV and cover letter (PDF), why it matched, the employer's emails and the agent's step-by-step notes from the form. |
| **Responses** | Company replies from Gmail sorted into action needed, interview, test, offer, recruiter, rejection; one click to open the email. |
| **Runs & schedule** | Start a full round or any single stage, watch the live log, stop it. Daily schedule (time + weekdays) and the Gmail watcher interval. |
| **Profile** | Every fact employers are told, as a form: identity, address per country, work rights, salary rules, languages, extra facts, allowed skills. Raw JSON editor for anything else. |
| **Job search** | Job titles and tiers, markets, freshness, boards, accepted and rejected places. Raw YAML editor too. |
| **Base CVs** | Edit the EN / FR / IT source CVs and preview them as PDF. |
| **Settings & keys** | Score threshold, applications per round, parallel browsers, model, headless mode, API keys (masked), prices, system check. |
| **Costs** | Measured token usage and cost per stage and per day, form-filling cost per attempt. |

The scheduler lives inside the app: it runs while `applypilot ui` is running and the machine is
awake. Every save keeps the previous file in `~/ApplyPilot/backups/`.

### Deploying

The app is a single process (FastAPI + a static frontend, no build step) and needs the same
tools as the pipeline: Python, Chromium, Node + Claude Code, optionally LaTeX.

- **On your Mac (recommended):** `applypilot ui`. The apply agent uses your real Chrome profile,
  so LinkedIn and Gmail sessions work, and your Claude subscription covers form filling.
- **On a server, with Docker:**
  ```bash
  export APPLYPILOT_UI_PASSWORD='choose-one' ANTHROPIC_API_KEY='sk-ant-…'
  docker compose up -d --build        # http://<server>:8765, basic auth with that password
  ```
  `./data` is mounted as the data folder and seeded with the example configs on first start;
  fill in your profile, CVs and keys from the UI. Browsers run headless and start without
  any logged-in sessions, so LinkedIn Easy Apply and Gmail-in-browser verification do not
  work there, and the apply agent is billed to the Anthropic API key.
- Listening on anything other than localhost requires `APPLYPILOT_UI_PASSWORD`; put it
  behind HTTPS (a reverse proxy or a tunnel) before exposing it to the internet.

## Command line

```bash
zsh ~/ApplyPilot/daily_run.sh              # one full round: discover → … → apply (until queue empty)

applypilot run discover                    # or step by step
applypilot run enrich score tailor cover pdf --min-score 7 --validation strict
applypilot apply --dry-run --limit 1       # watch one form get filled, nothing submitted
applypilot apply --limit 30 --workers 2 --model sonnet
applypilot apply --continuous              # keep applying as new jobs get tailored

applypilot status                          # counts per stage
applypilot dashboard                       # HTML overview of all jobs
applypilot inbox                           # check Gmail for replies (see below)
applypilot inbox --watch --interval 10     # keep checking every 10 minutes
applypilot inbox --show                    # print every company response so far
applypilot followups --limit 10            # do what the action-needed emails ask (verify, finish, consent)
```

Stop anything with `Ctrl+C` twice, or `pkill -INT -f "applypilot apply"`.

A round takes 1–3 hours depending on how many new postings there are; the Mac must stay
awake (`daily_run.sh` uses `caffeinate`). To run it every night, use cron/launchd or
any scheduler that can run a shell command on this machine.

Outputs in `~/ApplyPilot/`: `applypilot.db` (every job and its status), `tailored_resumes/`
(`.txt`, `.pdf`, `.tex`, the job text and a validation report per job), `cover_letters/`,
`logs/` (`nightly-<date>.log`, `worker-*.log` with the agent's step-by-step notes),
`responses.md`.

### Gmail response tracker

1. Turn on 2-step verification, then create an app password at
   <https://myaccount.google.com/apppasswords>.
2. In `api_keys.env`: `GMAIL_ADDRESS=you@gmail.com` and `GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx`.
3. `applypilot inbox --dry-run` to preview, then `applypilot inbox --watch`.

It only reads mail and adds labels (`ApplyPilot/Interview`, `ApplyPilot/Assessment`,
`ApplyPilot/Offer`, `ApplyPilot/Action needed`, `ApplyPilot/Recruiter`, `ApplyPilot/Rejected`,
`ApplyPilot/Applied`, `ApplyPilot/Job alerts`); it never deletes, archives, sends or marks
as read. Only messages that look job-related (ATS sender domains or job keywords in 10
languages) are sent to the LLM.

---

## Cost and token usage

Measured on 2026-11-02 with OpenAI **gpt-6-luna** ($0.10 / 1M input, $0.01 / 1M cached input,
$0.50 / 1M output, reasoning tokens billed as output) and the apply agent on **Claude Sonnet**
via Claude Code.

### LLM (OpenAI), per call

| Stage | Calls | Input tokens | Output tokens (of which reasoning) | Cost per call |
|---|---|---|---|---|
| score | 1 per posting | ~2,100 | ~270 (130) | ~$0.00035 |
| enrich (AI fallback only) | ~1 in 5 postings | ~7,700 | ~1,500 *(estimated)* | ~$0.0015 |
| tailor | ~1.3 per matched job (retries) | ~3,100 (≈1,100 cached) | ~2,450 (1,350) | ~$0.0014 |
| judge | 1 per tailor attempt | ~2,900 | ~350 (330) | ~$0.00046 |
| cover letter | 1 per matched job | ~3,200 (≈950 cached) | ~1,550 (1,280) | ~$0.0010 |
| inbox classify | 1 per job-looking email | ~1,000 | ~150 *(estimated)* | ~$0.0002 |

So: **~$0.00035 to evaluate a posting, ~$0.004 to prepare one application** (CV + judge + letter).

### Apply agent (Claude)

Measured cost per attempt ranged **$0.10–$0.80** (API-equivalent); a full multi-page
Workday application was $0.80 / 142 s. Average time: **89 s per submitted application,
39 s per failed attempt** → roughly **$0.50 per submission, $0.20 per failure**.

On a Claude Pro/Max subscription this is **not billed per token**: it consumes the plan's
5-hour usage window instead. Two Sonnet workers used up a window in about four hours.
The launcher pauses 15 minutes and retries when that happens. `APPLY_MODEL=haiku` is
several times cheaper but fills complex forms less reliably.

### A real night (2026-11-02, 25 markets, first run)

| Item | Volume | Cost |
|---|---|---|
| Postings scored | 5,645 | ~$2.00 |
| AI enrichment fallbacks | ~1,025 | ~$1.50 |
| CVs tailored + judged | 267 | ~$0.65 |
| Cover letters | 267 | ~$0.25 |
| **OpenAI total** | | **≈ $4.40** |
| Applications submitted / failed | 169 / ~250 attempts | ≈ $135 API-equivalent (subscription: ~1 usage window) |

A steady daily round (≈ 300–400 new postings, 10–30 matches) costs about **$0.20 in
OpenAI** plus **$5–15 API-equivalent of Claude**. Exact numbers for your own runs:
`logs/llm_usage.jsonl` and `logs/apply_usage.jsonl`.

---

## Safety rules (always on)

- No invented skills, tools, numbers, titles, seniority or certifications. Programmatic
  checks + an AI judge; strict mode discards anything flagged.
- CV is one page, includes every project from the base CV, headings in the job's language.
- Facts come only from `profile.json`; missing facts park the job (`missing_required_*`). Forced permit-type
  choices follow `work_authorization.permit_choice_rule` (never claims citizenship or a Blue Card).
- Preference questions (relocation, travel, start date, contract type) get the answer most
  likely to advance the application.
- One application per role per employer (reposts across cities, boards, languages are skipped).
- Never: video/selfie/ID verification, payment details, Google/Microsoft single sign-on,
  emails sent on your behalf, freelance-marketplace profiles. Password resets only on employer
  career sites where an account already exists under your email (never Google/Microsoft/LinkedIn).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Many `no_result_line` failures | Claude usage limit reached before this fork's pause logic; reset those rows (`apply_status=NULL`) |
| `login_issue` | Sign-in and the password reset both failed on that career site (e.g. reset email never arrived) |
| `sso_required` | Site only offers Google/Microsoft login (refused by design); for LinkedIn, log in to LinkedIn in Chrome and delete `~/ApplyPilot/chrome-workers/` |
| `missing_required_<x>` | Add `<x>` to `personal.extra_facts` (or the matching field) in `profile.json` |
| CV rendered with HTML instead of LaTeX | See `<cv>.tex.log` next to the PDF; HTML is the automatic fallback |
| Two apply processes | Only run one `applypilot apply` at a time |

## License

AGPL-3.0, same as upstream. See [LICENSE](LICENSE).
