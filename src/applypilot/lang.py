"""Language detection and localisation helpers (local patch).

Picks the base resume language per job (en / fr / it), localises PDF section
titles and cover-letter salutations. Detection is a cheap stop-word count on
the job description, with the job location as a fallback.
"""

from __future__ import annotations

import re
from collections import Counter

DEFAULT_LANG = "en"
LANG_NAMES = {"en": "English", "fr": "French", "it": "Italian"}

# Frequent function words that are (nearly) unique to each language.
_MARKERS: dict[str, list[str]] = {
    "fr": ["des", "et", "vous", "nous", "pour", "dans", "une", "est", "avec", "sur", "au", "aux",
           "qui", "que", "ou", "votre", "vos", "nos", "notre", "être", "sont", "les", "poste",
           "équipe", "expérience", "chez", "candidat", "recherchons", "missions"],
    "it": ["di", "che", "per", "con", "una", "del", "della", "dei", "delle", "sono", "nel", "nella",
           "alla", "ed", "anche", "più", "gli", "siamo", "nostro", "nostra", "degli", "sul", "ruolo",
           "esperienza", "candidato", "cerchiamo", "azienda", "lavoro"],
    "en": ["the", "and", "of", "to", "with", "you", "we", "for", "our", "is", "are", "will", "that",
           "as", "your", "be", "have", "this", "team", "experience", "role", "looking", "join"],
}

_LOCATION_MARKERS: dict[str, list[str]] = {
    "fr": ["france", "paris", "lyon", "marseille", "toulouse", "nantes", "bordeaux", "lille", "nice",
           "sophia antipolis", "grenoble", "montpellier", "strasbourg", "rennes", "île-de-france",
           "ile-de-france"],
    "it": ["italy", "italia", "milan", "milano", "rome", "roma", "turin", "torino", "bologna",
           "florence", "firenze", "naples", "napoli", "padova", "padua", "genova", "genoa", "verona",
           "urbino", "pesaro", "ancona", "lombardia", "lombardy"],
}

_WORD_RE = re.compile(r"[a-zàâçéèêëîïôûùüÿœæ]+")


def detect_text_language(text: str | None, min_hits: int = 8, margin: float = 1.3) -> str | None:
    """Return 'en', 'fr' or 'it' when the text is clearly in one language, else None.

    Bilingual postings (common in Italy and France) come back None so the
    caller can fall back to the job location.
    """
    if not text:
        return None
    counts = Counter(_WORD_RE.findall(text.lower()))
    scores = {lang: sum(counts.get(m, 0) for m in markers) for lang, markers in _MARKERS.items()}
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_lang, best = ordered[0]
    second = ordered[1][1]
    if best < min_hits:
        return None
    if second and best < margin * second:
        return None
    return best_lang


def detect_location_language(location: str | None) -> str | None:
    """Map a job location string to 'fr' or 'it' when it names a known place."""
    if not location:
        return None
    loc = location.lower()
    for lang, markers in _LOCATION_MARKERS.items():
        if any(m in loc for m in markers):
            return lang
    return None


def detect_job_language(job: dict) -> str:
    """Language to use for a job: description language first, then location, else English."""
    text = job.get("full_description") or job.get("description") or ""
    lang = detect_text_language(text)
    if lang is None:
        lang = detect_location_language(job.get("location"))
    return lang or DEFAULT_LANG


# Section titles rendered in the PDF. The .txt format keeps the English
# ALL-CAPS keys internally; only the printed heading changes.
SECTION_TITLES: dict[str, dict[str, str]] = {
    "en": {"SUMMARY": "Summary", "TECHNICAL SKILLS": "Technical Skills", "EXPERIENCE": "Experience",
           "PROJECTS": "Projects", "EDUCATION": "Education"},
    "fr": {"SUMMARY": "Profil", "TECHNICAL SKILLS": "Compétences techniques",
           "EXPERIENCE": "Expérience professionnelle", "PROJECTS": "Projets", "EDUCATION": "Formation"},
    "it": {"SUMMARY": "Profilo", "TECHNICAL SKILLS": "Competenze tecniche",
           "EXPERIENCE": "Esperienza professionale", "PROJECTS": "Progetti", "EDUCATION": "Formazione"},
}

COVER_SALUTATIONS: dict[str, str] = {
    "en": "Dear Hiring Manager,",
    "fr": "Madame, Monsieur,",
    "it": "Gentile Responsabile delle Risorse Umane,",
}

# Lower-case prefixes the validator accepts as a letter opening.
SALUTATION_PREFIXES: dict[str, tuple[str, ...]] = {
    "en": ("dear",),
    "fr": ("madame", "monsieur", "bonjour", "chère", "cher "),
    "it": ("gentile", "gentili", "spett", "egregi", "buongiorno", "alla cortese"),
}


_COUNTRY_URL_HINTS: dict[str, tuple[str, ...]] = {
    "FR": ("fr.indeed.com", "/france", "france-", "-france"),
    "GULF": ("ae.indeed.com", "qa.indeed.com", "sa.indeed.com"),
}

_GULF_MARKERS = ("united arab emirates", "dubai", "abu dhabi", "sharjah", "qatar", "doha",
                 "saudi", "riyadh", "jeddah", "dammam", "bahrain", "kuwait", "oman", "muscat")
_GULF_SHORT = ("uae", "ksa", "gcc")


def detect_job_country(job: dict) -> str | None:
    """Best-effort ISO country code of the job (currently only "FR" matters).

    Looks at the location text first, then at the posting URL.
    """
    loc = (job.get("location") or "").lower()
    url = (job.get("url") or "").lower()
    if any(m in loc for m in _GULF_MARKERS) or any(re.search(rf"(?<![a-z]){t}(?![a-z])", loc) for t in _GULF_SHORT):
        return "GULF"
    if any(h in url for h in _COUNTRY_URL_HINTS["GULF"]):
        return "GULF"
    if any(m in loc for m in _LOCATION_MARKERS["fr"]):
        return "FR"
    if any(h in url for h in _COUNTRY_URL_HINTS["FR"]):
        return "FR"
    return None
