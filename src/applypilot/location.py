"""Per-job candidate location (local patch).

The candidate can present a different home location per job country (e.g. one city
for French postings, another for the rest), always open to relocation. The base
resume header, the tailored resume header, the cover letter and the apply
agent's form answers all go through here.
"""

from __future__ import annotations

import copy
import re

from applypilot.lang import DEFAULT_LANG, detect_job_country

RELOCATION_TEXT = {
    "en": "Currently in {label}; open to relocation anywhere in Europe and to fully remote roles",
    "fr": "Actuellement à {label} ; ouvert à la mobilité partout en Europe et au télétravail",
    "it": "Attualmente a {label}; disponibile al trasferimento in tutta Europa e al lavoro da remoto",
}


def resolve_candidate_location(profile: dict, job: dict, lang: str = DEFAULT_LANG) -> dict:
    """Return {city, province_state, country, label, relocation} for this job."""
    personal = profile.get("personal", {})
    table = personal.get("location_by_country", {})
    country = detect_job_country(job)
    entry = table.get(country) if country else None
    if entry is None:
        entry = table.get("default") or {
            "city": personal.get("city", ""),
            "province_state": personal.get("province_state", ""),
            "country": personal.get("country", ""),
            "label": {},
        }
    labels = entry.get("label", {})
    label = labels.get(lang) or labels.get("en") or f"{entry.get('city', '')}, {entry.get('country', '')}".strip(", ")
    relocation = RELOCATION_TEXT.get(lang, RELOCATION_TEXT["en"]).format(label=label)
    return {
        "city": entry.get("city", ""),
        "province_state": entry.get("province_state", ""),
        "country": entry.get("country", ""),
        "label": label,
        "relocation": relocation,
        "country_code": country,
        "mention_arabic": country == "GULF",
    }


LANGUAGES_BASE = {
    "en": "English (C1), French (C1), Italian (B1)",
    "fr": "Anglais (C1), Français (C1), Italien (B1)",
    "it": "inglese (C1), francese (C1), italiano (B1)",
}
ARABIC_NATIVE = {"en": "Arabic (native)", "fr": "Arabe (natif)", "it": "arabo (madrelingua)"}


def _language_config() -> tuple[dict, dict]:
    """Language lines from profile.json ("languages"), falling back to the built-in defaults."""
    try:
        from applypilot.config import load_profile
        cfg = load_profile().get("languages") or {}
        return (cfg.get("listed") or LANGUAGES_BASE), (cfg.get("gulf_only") or ARABIC_NATIVE)
    except Exception:
        return LANGUAGES_BASE, ARABIC_NATIVE


def spoken_languages_for(location: dict, lang: str = DEFAULT_LANG) -> str:
    """Languages to present, in the resume's language; 'gulf_only' languages only for Gulf postings."""
    listed, gulf = _language_config()
    base = listed.get(lang) or listed.get("en") or next(iter(listed.values()), "")
    extra = gulf.get(lang) or gulf.get("en") or ""
    if location.get("mention_arabic") and extra:
        return f"{base}, {extra}"
    return base


def profile_for_job(profile: dict, job: dict, lang: str = DEFAULT_LANG) -> dict:
    """Deep copy of the profile with the location fields set for this job."""
    loc = resolve_candidate_location(profile, job, lang)
    p = copy.deepcopy(profile)
    p.setdefault("personal", {})
    p["personal"]["city"] = loc["city"]
    p["personal"]["province_state"] = loc["province_state"]
    p["personal"]["country"] = loc["country"]
    # Street address follows the per-country location; it is blank where no address is known,
    # so a form never mixes one country's city with another country's street.
    entry = (profile.get("personal", {}).get("location_by_country", {})
             .get(loc.get("country_code") or "default") or {})
    if "address" in entry:
        p["personal"]["address"] = entry.get("address", "")
        p["personal"]["postal_code"] = entry.get("postal_code", "")
    p.setdefault("availability", {})["relocation"] = loc["relocation"]
    facts = p["personal"].setdefault("extra_facts", {})
    facts["languages"] = (
        spoken_languages_for(loc)
        + ("" if loc["mention_arabic"] else " (do not mention Arabic)")
        + "; German, Spanish, Dutch, Polish and any other language: none (answer 'none' / lowest level when a form requires a level)"
    )
    if loc["country_code"] == "GULF":
        # Tunisian national: Gulf roles need an employer-sponsored employment visa.
        p["work_authorization"] = {
            "legally_authorized_to_work": "Not yet for this country; requires employer visa sponsorship (standard for Gulf hires)",
            "require_sponsorship": "Yes, employment visa sponsorship for UAE/Qatar/Saudi Arabia",
            "work_permit_type": "Tunisian national, authorized to work in Europe; needs an employer-sponsored visa for Gulf roles",
        }
    return p


_CONTACT_LINE = re.compile(r"^(?P<loc>[^|\n]+?)\s*\|\s*(?P<rest>.*\+\d.*|.*@.*)$", re.M)


def localize_resume_header(text: str, label: str) -> str:
    """Replace the location segment of the base resume's contact line with `label`.

    The base CVs have a header line like "City, Country | +39 ... | email | linkedin".
    Only the first such line within the first six lines is touched.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines[:6]):
        if "|" in line and ("@" in line or "+" in line):
            parts = [p.strip() for p in line.split("|")]
            if parts and "@" not in parts[0] and "+" not in parts[0]:
                parts[0] = label
                lines[i] = " | ".join(parts)
            break
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
