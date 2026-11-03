"""Charter-style LaTeX resume renderer (local patch, 2026-11-03).

Reproduces the look of Oussama's own CVs (pdfTeX + Bitstream Charter: small-caps name and
section headings with a thin rule, bold employer / right-aligned dates, italic role / location,
bold key phrases inside bullets). It renders the resume TEXT in the same structure as the base
CVs (resume_<lang>.txt):

    NAME
    Subtitle
    contact | contact | contact

    PROFILE                      (any ALL-CAPS heading, in the CV's language)
    paragraph

    PROFESSIONAL EXPERIENCE
    Employer | Role | Location | Dates
    - bullet with **bold** phrases
    ...
    EDUCATION
    Institution | Degree | Location | Dates
    PROJECTS
    Project | tech, tech
    - bullet
    SKILLS
    Label: items

`**text**` renders bold. Shrinks a few steps until the PDF is one page; returns False when it
cannot (caller falls back to another renderer).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from applypilot.scoring.latex_resume import _pdflatex, tex

log = logging.getLogger(__name__)

# (body pt, baseline pt, top/bottom margin cm, before-section pt, after-rule pt, bullet sep pt, name gap pt)
_SCALES = (
    # user tweak 2026-11-03: less space between sections, slightly bigger text than the originals
    (9.6, 11.5, 1.00, 8.0, 3.0, 1.0, 3.0),
    (9.4, 11.2, 0.95, 7.5, 3.0, 0.5, 2.0),
    (9.2, 11.0, 0.90, 7.0, 2.5, 0.5, 2.0),
    (9.0, 10.8, 0.90, 7.0, 2.5, 0.0, 2.0),   # the originals' size
    (8.8, 10.4, 0.85, 6.5, 2.5, 0.0, 1.0),
    (8.5, 10.0, 0.80, 6.0, 2.0, 0.0, 1.0),
    (8.2, 9.6, 0.75, 6.0, 2.0, 0.0, 1.0),
    (7.9, 9.3, 0.70, 5.0, 2.0, 0.0, 1.0),
)

_DATE_RE = re.compile(r"(\d{1,2}/\d{4}|\d{4})\s*[-–]\s*(\d{1,2}/\d{4}|\d{4}|present|présent|oggi|now|aujourd'hui|attuale)", re.I)
_HEADING_RE = re.compile(r"^[A-ZÀ-ÝŒ][A-ZÀ-ÝŒ &'’/-]{3,}$")
_LABEL_RE = re.compile(r"^([^:|]{2,32}?)\s?:\s*(.*)$")

_LANG_LABELS = ("languages", "langues", "lingue", "sprachen", "idiomas")
_CERT_LABELS = ("certification", "certifications", "certificazione", "certificazioni", "certificaciones")


def fakesc(text: str, big: str, small: str) -> str:
    """Small caps the way the original CVs render them: initial at `big`, the rest as capitals at `small`."""
    words = []
    for w in (text or "").split(" "):
        if not w:
            continue
        words.append(rf"{{{big} {tex(w[0].upper())}}}{{{small} {tex(w[1:].upper())}}}" if len(w) > 1
                     else rf"{{{big} {tex(w.upper())}}}")
    return " ".join(words)


_RANGE_RE = re.compile(r"(\d{4}|\d{1,2}/\d{4})\s*-\s*(?=\d|present|présent|oggi|now|attuale|aujourd)", re.I)


def _dash(text: str) -> str:
    """'11/2024 - present' -> '11/2024 – present' (the original CVs use an en dash)."""
    return _RANGE_RE.sub(lambda m: m.group(1) + " – ", text or "")


def rich(text: str) -> str:
    """Escape text for LaTeX, turning **x** into \\textbf{x}; straight "quotes" become curly ones."""
    text = re.sub(r'"([^"]*)"', "“\\1”", text or "")
    parts = re.split(r"\*\*(.+?)\*\*", text)
    out = []
    for i, p in enumerate(parts):
        out.append(rf"\textbf{{{tex(p)}}}" if i % 2 else tex(p))
    return "".join(out)


def _contact(contact: str) -> str:
    items = []
    for p in [x.strip() for x in (contact or "").split("|") if x.strip()]:
        if "@" in p and " " not in p:
            items.append(rf"\href{{mailto:{p}}}{{{tex(p)}}}")
        elif p.startswith("http") or "linkedin.com" in p or "github.com" in p:
            url = p if p.startswith("http") else "https://" + p
            items.append(rf"\href{{{url}}}{{{tex(p)}}}")
        else:
            items.append(tex(p))
    return r" $|$ ".join(items)


def parse(text: str) -> dict:
    """Split the resume text into header + ordered sections (heading text kept as written)."""
    lines = [l.rstrip() for l in text.strip().splitlines()]
    header: list[str] = []
    i = 0
    while i < len(lines) and len(header) < 3:
        if lines[i].strip():
            header.append(lines[i].strip())
        i += 1
    sections: list[tuple[str, list[str]]] = []
    cur: list[str] | None = None
    for l in lines[i:]:
        s = l.strip()
        if s and _HEADING_RE.match(s) and not s.startswith("-"):
            cur = []
            sections.append((s, cur))
        elif cur is not None:
            cur.append(l)
    return {"name": header[0] if header else "", "title": header[1] if len(header) > 1 else "",
            "contact": header[2] if len(header) > 2 else "", "sections": sections}


def _split_header(line: str) -> tuple[str, str, str, str]:
    """'A | B | C | D' -> (left, right-top, left-italic, right-italic) following the CV layout."""
    parts = [p.strip() for p in line.split("|")]
    if len(parts) >= 4:
        return parts[0], parts[3], parts[1], parts[2]
    if len(parts) == 3:
        if _DATE_RE.search(parts[2]):
            return parts[0], parts[2], parts[1], ""
        return parts[0], "", parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], "", ""
    return parts[0], "", "", ""


def _entries(lines: list[str], bullet_sep: float, italic_right: bool) -> str:
    """Experience / projects / education blocks."""
    out: list[str] = [r"\begin{entries}"]
    bullets: list[str] = []

    def flush_bullets() -> None:
        if bullets:
            out.append(r"\begin{itemize}")
            out.extend(rf"\item {rich(b)}" for b in bullets)
            out.append(r"\end{itemize}")
            bullets.clear()

    for l in lines:
        s = l.strip()
        if not s:
            continue
        if s.startswith("- ") or s.startswith("• "):
            bullets.append(s[2:].strip())
            continue
        flush_bullets()
        left, right, sub_l, sub_r = _split_header(s)
        right_tex = rf"{{\itshape {rich(right)}}}" if (italic_right and right) else rich(_dash(right))
        out.append(rf"\entryline{{\textbf{{{rich(left)}}}}}{{{right_tex}}}")
        if sub_l or sub_r:
            out.append(rf"\entryline{{{{\itshape {rich(sub_l)}}}}}{{{{\itshape {rich(sub_r)}}}}}")
    flush_bullets()
    out.append(r"\end{entries}")
    return "\n".join(out)


def _skills(lines: list[str]) -> str:
    rows: list[tuple[str, str, bool]] = []   # (label, value, french_colon)
    for l in lines:
        s = l.strip()
        if not s:
            continue
        m = _LABEL_RE.match(s)
        if m:
            rows.append((m.group(1).strip(), m.group(2).strip(), " :" in s[:40]))
        else:
            rows.append(("", s, False))
    lang_row = next((r for r in rows if r[0].lower() in _LANG_LABELS), None)
    cert_row = next((r for r in rows if r[0].lower() in _CERT_LABELS), None)

    def fmt(r: tuple[str, str, bool]) -> str:
        label, value, fr = r
        if not label:
            return rich(value)
        colon = r"\,:" if fr else ":"
        return rf"\textbf{{{rich(label)}{colon}}} {rich(value)}"

    out: list[str] = []
    for r in rows:
        if r is cert_row and lang_row is not None:
            continue
        line = fmt(r)
        if r is lang_row and cert_row is not None:
            line += r"\hfill " + fmt(cert_row)
        out.append(line)
    return r"\begin{entries}" + "\n" + "\n\n".join(out) + "\n" + r"\end{entries}"


def _kind(heading: str, lines: list[str]) -> str:
    h = heading.lower()
    if any(k in h for k in ("skill", "compétence", "competenz", "kompetenz")):
        return "skills"
    if any(k in h for k in ("educ", "formation", "formazione", "studi", "ausbildung")):
        return "education"
    if any(" | " in l for l in lines) or any(l.strip().startswith("- ") for l in lines):
        return "entries"
    return "paragraph"


def build_charter_latex(text: str, scale: tuple = _SCALES[0]) -> str:
    size, base, vmargin, before, after, bsep, namegap = scale
    r = parse(text)
    body: list[str] = []
    for heading, lines in r["sections"]:
        kind = _kind(heading, lines)
        body.append(rf"\section{{{fakesc(heading, r'\large', r'\small')}}}")
        if kind == "skills":
            body.append(_skills(lines))
        elif kind == "education":
            body.append(_entries(lines, bsep, italic_right=False))
        elif kind == "entries":
            # projects carry "Project | tech" -> tech in italics on the right; jobs carry dates (upright)
            italic_right = all(len(l.split("|")) <= 2 for l in lines if " | " in l)
            body.append(_entries(lines, bsep, italic_right=italic_right))
        else:
            para = " ".join(l.strip() for l in lines if l.strip())
            body.append(rich(para))
    return rf"""\documentclass[10pt,a4paper]{{article}}
\usepackage[T1]{{fontenc}}
\usepackage[utf8]{{inputenc}}
\usepackage{{charter}}
\usepackage[a4paper,left=1.48cm,right=1.72cm,top={vmargin}cm,bottom={vmargin}cm]{{geometry}}
\usepackage{{enumitem}}
\usepackage{{titlesec}}
\usepackage[hidelinks]{{hyperref}}
\pagestyle{{empty}}
\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{0pt}}
\titleformat{{\section}}{{\large}}{{}}{{0pt}}{{}}[\vspace{{-0.85em}}\rule{{\linewidth}}{{0.4pt}}]
\titlespacing*{{\section}}{{0pt}}{{{before}pt}}{{{after}pt}}
\setlist[itemize]{{leftmargin=11pt,labelsep=4.5pt,label=\textbullet,topsep=0pt,itemsep={bsep}pt,parsep=0pt,partopsep=0pt}}
\newenvironment{{entries}}{{\begin{{list}}{{}}{{\setlength\leftmargin{{3pt}}\setlength\rightmargin{{6pt}}\setlength\itemsep{{0pt}}\setlength\parsep{{0pt}}\setlength\topsep{{0pt}}\setlength\partopsep{{0pt}}\setlength\itemindent{{0pt}}\setlength\labelwidth{{0pt}}\setlength\labelsep{{0pt}}\setlength\listparindent{{0pt}}}}\item[]}}{{\end{{list}}}}
\newcommand{{\entryline}}[2]{{\par\noindent #1\hfill #2\par}}
\begin{{document}}
\fontsize{{{size}}}{{{base}}}\selectfont
\begin{{center}}
{fakesc(r['name'], r'\Huge', r'\huge')}\\[{namegap}pt]
{{\itshape {tex(r['title'])}}}\\[{namegap}pt]
{_contact(r['contact'])}
\end{{center}}
\vspace{{-4pt}}
{chr(10).join(body)}
\end{{document}}
"""


def _pages(pdf: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(pdf)).pages)


def render_charter_resume(text: str, out_pdf: Path) -> bool:
    """Compile `text` into out_pdf (one page). Keeps the .tex next to the PDF. False = could not."""
    pdflatex = _pdflatex()
    if not pdflatex:
        return False
    out_pdf = Path(out_pdf)
    with tempfile.TemporaryDirectory(prefix="ap_charter_") as tmp:
        tmpd = Path(tmp)
        for scale in _SCALES:
            src = tmpd / "resume.tex"
            src.write_text(build_charter_latex(text, scale), encoding="utf-8")
            try:
                proc = subprocess.run([pdflatex, "-interaction=nonstopmode", "-halt-on-error", "resume.tex"],
                                      cwd=tmpd, capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", timeout=60)
            except Exception as exc:
                log.warning("Charter LaTeX render failed (%s)", exc)
                return False
            pdf = tmpd / "resume.pdf"
            if proc.returncode != 0 or not pdf.exists():
                err = next((l for l in proc.stdout.splitlines() if l.startswith("!")), "unknown error")
                log.warning("Charter LaTeX compile error: %s", err)
                out_pdf.with_suffix(".tex.log").write_text(proc.stdout[-4000:], encoding="utf-8")
                return False
            if _pages(pdf) == 1:
                shutil.copy(pdf, out_pdf)
                shutil.copy(src, out_pdf.with_suffix(".tex"))
                return True
        log.warning("Charter resume does not fit one page at the smallest scale")
        return False
