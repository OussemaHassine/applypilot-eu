"""LaTeX resume renderer (local patch, 2026-11-02).

Renders the parsed tailored resume with pdflatex in a classic one-column
engineering-resume layout. It is strictly optional: `render_latex_resume`
returns False on any problem (no pdflatex, compile error, cannot fit one
page), and the caller then falls back to the HTML renderer. Nothing about the
tailoring or the apply stage depends on LaTeX.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_PDFLATEX_CANDIDATES = ("pdflatex", "/Library/TeX/texbin/pdflatex")

# (font size pt, baseline pt, side margin in, top margin in, item sep pt)
_LAYOUTS = (
    (10.5, 12.6, 0.55, 0.45, 1.0),
    (10.0, 12.0, 0.5, 0.4, 0.6),
    (9.5, 11.3, 0.45, 0.35, 0.3),
    (9.0, 10.7, 0.4, 0.3, 0.0),
    (8.6, 10.2, 0.38, 0.28, 0.0),
)

_ESCAPES = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    "<": r"\textless{}", ">": r"\textgreater{}", "–": "--", "—": "---",
    "’": "'", "‘": "`", "“": "``", "”": "''", " ": "~",
}
_ESC_RE = re.compile("|".join(re.escape(k) for k in _ESCAPES))


def tex(text: str) -> str:
    """Escape plain text for LaTeX."""
    return _ESC_RE.sub(lambda m: _ESCAPES[m.group(0)], text or "")


def _pdflatex() -> str | None:
    for cand in _PDFLATEX_CANDIDATES:
        path = shutil.which(cand) or (cand if Path(cand).exists() else None)
        if path:
            return path
    return None


def _contact_tex(contact: str) -> str:
    parts = []
    for p in [x.strip() for x in (contact or "").split("|") if x.strip()]:
        if "@" in p and " " not in p:
            parts.append(rf"\href{{mailto:{p}}}{{{tex(p)}}}")
        elif p.startswith("http") or "linkedin.com" in p or "github.com" in p:
            url = p if p.startswith("http") else "https://" + p
            label = re.sub(r"^https?://(www\.)?", "", p).rstrip("/")
            parts.append(rf"\href{{{url}}}{{{tex(label)}}}")
        else:
            parts.append(tex(p))
    return r" \enspace$\cdot$\enspace ".join(parts)


def _entries_tex(entries: list[dict]) -> str:
    out = []
    for e in entries:
        title = tex(e.get("title", ""))
        sub = tex(e.get("subtitle", ""))
        block = [rf"\entryhead{{{title}}}{{{sub}}}"]
        if e.get("bullets"):
            block.append(r"\begin{itemize}")
            block += [rf"\item {tex(b)}" for b in e["bullets"]]
            block.append(r"\end{itemize}")
        out.append("\n".join(block))
    return "\n".join(out)


def build_latex(resume: dict, titles: dict, layout: tuple, parse_skills, parse_entries) -> str:
    """Build the .tex source for a parsed resume (see pdf.parse_resume)."""
    size, base, side, top, itemsep = layout
    s = resume["sections"]
    body: list[str] = []

    if "SUMMARY" in s:
        body.append(rf"\section{{{tex(titles['SUMMARY'])}}}" + "\n" + tex(s["SUMMARY"].strip()))
    if "TECHNICAL SKILLS" in s:
        rows = [rf"\textbf{{{tex(c)}:}} {tex(v)}" for c, v in parse_skills(s["TECHNICAL SKILLS"])]
        body.append(rf"\section{{{tex(titles['TECHNICAL SKILLS'])}}}" + "\n" + r" \\" .join(rows))
    if "EXPERIENCE" in s:
        body.append(rf"\section{{{tex(titles['EXPERIENCE'])}}}" + "\n" + _entries_tex(parse_entries(s["EXPERIENCE"])))
    if "PROJECTS" in s and s["PROJECTS"].strip():
        body.append(rf"\section{{{tex(titles['PROJECTS'])}}}" + "\n" + _entries_tex(parse_entries(s["PROJECTS"])))
    if "EDUCATION" in s:
        lines = [tex(l.strip()) for l in s["EDUCATION"].strip().splitlines() if l.strip()]
        body.append(rf"\section{{{tex(titles['EDUCATION'])}}}" + "\n" + r" \\" .join(lines))

    location = tex(resume.get("location", ""))
    return rf"""\documentclass[10pt]{{article}}
\usepackage[T1]{{fontenc}}
\usepackage[utf8]{{inputenc}}
\usepackage{{lmodern}}
\usepackage[a4paper,left={side}in,right={side}in,top={top}in,bottom={top}in]{{geometry}}
\usepackage{{enumitem}}
\usepackage{{titlesec}}
\usepackage[hidelinks]{{hyperref}}
\usepackage{{xcolor}}
\definecolor{{accent}}{{RGB}}{{31,58,96}}
\pagestyle{{empty}}
\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{0pt}}
\fontsize{{{size}}}{{{base}}}\selectfont
\titleformat{{\section}}{{\color{{accent}}\normalsize\bfseries\scshape}}{{}}{{0pt}}{{}}[\vspace{{-0.55em}}\color{{accent}}\rule{{\linewidth}}{{0.5pt}}]
\titlespacing*{{\section}}{{0pt}}{{0.65em}}{{0.35em}}
\setlist[itemize]{{leftmargin=1.1em,label=\textbullet,topsep=1pt,itemsep={itemsep}pt,parsep=0pt,partopsep=0pt}}
\newcommand{{\entryhead}}[2]{{\par\vspace{{0.25em}}\textbf{{#1}}\ifx&#2&\else\\{{\small\itshape #2}}\fi\par}}
\begin{{document}}
\fontsize{{{size}}}{{{base}}}\selectfont
\begin{{center}}
{{\LARGE\bfseries {tex(resume.get('name', ''))}}}\\[2pt]
{{\large {tex(resume.get('title', ''))}}}\\[2pt]
{{\small {location}{r' \enspace$\cdot$\enspace ' if location else ''}{_contact_tex(resume.get('contact', ''))}}}
\end{{center}}
\vspace{{-0.4em}}
{chr(10).join(body)}
\end{{document}}
"""


def render_latex_resume(resume: dict, titles: dict, out_pdf: Path, parse_skills, parse_entries,
                        count_pages) -> bool:
    """Compile the resume with pdflatex into out_pdf, shrinking until it fits one page.

    Returns True on success; False means the caller should use the HTML renderer.
    """
    pdflatex = _pdflatex()
    if not pdflatex:
        return False
    out_pdf = Path(out_pdf)
    with tempfile.TemporaryDirectory(prefix="ap_tex_") as tmp:
        tmpd = Path(tmp)
        for layout in _LAYOUTS:
            src = tmpd / "resume.tex"
            src.write_text(build_latex(resume, titles, layout, parse_skills, parse_entries), encoding="utf-8")
            try:
                proc = subprocess.run(
                    [pdflatex, "-interaction=nonstopmode", "-halt-on-error", "resume.tex"],
                    cwd=tmpd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                )
            except Exception as exc:  # timeout, missing binary, ...
                log.warning("LaTeX render failed (%s); falling back to HTML", exc)
                return False
            pdf = tmpd / "resume.pdf"
            if proc.returncode != 0 or not pdf.exists():
                err = next((l for l in proc.stdout.splitlines() if l.startswith("!")), "unknown error")
                log.warning("LaTeX compile error (%s); falling back to HTML", err)
                (out_pdf.with_suffix(".tex.log")).write_text(proc.stdout[-4000:], encoding="utf-8")
                return False
            if count_pages(pdf) == 1:
                shutil.copy(pdf, out_pdf)
                shutil.copy(src, out_pdf.with_suffix(".tex"))  # keep the source next to the PDF
                return True
        log.warning("LaTeX resume does not fit one page at the smallest layout; falling back to HTML")
        return False
