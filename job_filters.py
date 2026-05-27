"""
job_filters.py — Shared filter chain for both Adzuna and Swiss employer pipelines.

Exposes apply_filter_chain(job_dict) -> (keep: bool, reject_reason: str)
where job_dict has keys: title, location, jd_body (optional), workmode (optional).

Filter chain:
  1. CH-only location (empty -> reject)
  2. Onsite workmode: must be in commute zone (Basel/Aargau/Zürich). Hybrid/remote/unknown OK.
  3. Function reject: installer/technician/monteur titles UNLESS title also matches PM pattern
  4. Junior/intern/assistant title reject
  5. Tech focus: hydropower/wind/HVAC etc. in TITLE -> reject
  6. Language: if JD body provided, reject if dominantly French/Italian OR if FR/IT
     stated as mandatory without optional marker
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from html import unescape

# ============================================================================
# Filter configuration
# ============================================================================

KEYWORDS_FUNCTION = [
    "project manager", "projektmanager", "projektleiter", "projektleiterin",
    "chef de projet", "cheffe de projet", "responsable de projet",
    "responsable de projets", "owner's engineer", "owner engineer",
    "technical project", "technischer projektleiter",
    "procurement", "beschaffung", "tender", "tendering", "appel d'offres",
    "buyer", "einkauf", "einkäufer", "achats", "acheteur",
]

TECH_REJECT = [
    "hydro", "wasserkraft", "hydroélectri", "hydropower", "hydraulique",
    "pumped storage",
    "wind", "windkraft", "éolien", "eolien",
    "nuclear", "kernkraft", "nucléaire",
    "gas-fired", "gas turbine", "lng", "lpg",
    "thermal", "thermoélectri", "kohle", "coal",
    "hvac", "cvc", "chauffage", "klima", "klimat", "heating",
    "sanitär", "sanitaire",
]

FUNCTION_REJECT_TITLE = [
    "electrician", "electricien", "électricien", "elektroinstallateur",
    "elektriker", "elektroinstallation",
    "monteur", "monteuse", "montage",
    "netzelektriker", "servicemonteur", "servicetechniker",
    "automaticien", "automatikerin", "automatiker",
    "fitter", "installer",
    "foreman", "contremaître", "contremaitre",
    "dessinateur", "dessinatrice", "zeichner", "zeichnerin",
    "draftsman",
    "chef de chantier", "bauleiter elektro", "bauleiter installation",
]

JUNIOR_REJECT = [
    "intern", "internship", "praktikant", "praktikum", "praktikantin",
    "trainee", "apprentice", "apprenti", "apprentie", "lehrling",
    "lehrstelle", "stagiaire", "stage",
    "junior",
    "assistant", "assistente", "assistentin",
]

SWISS_LOCATIONS = {
    "aarau", "baden", "basel", "bellinzona", "bern", "biel",
    "chur", "frauenfeld", "kloten", "luzern", "lucerne",
    "olten", "schaffhausen", "solothurn", "st gallen", "st. gallen", "sankt gallen",
    "thun", "winterthur", "zug", "zurich", "zürich",
    "niedergösgen", "gösgen", "ostermundigen", "ittigen", "wallisellen",
    "geneva", "genève", "geneve", "genf", "lausanne", "fribourg",
    "neuchâtel", "neuchatel", "neuenburg", "sion", "sitten",
    "martigny", "monthey", "yverdon", "delémont", "delemont",
    "morges", "vevey", "montreux", "nyon", "renens",
    "matran", "granges-paccot", "granges", "boudry", "tavannes", "guin",
    "boudevilliers", "epagny", "payerne", "sâles", "murten", "morat",
    "château-d'oex", "château",
    "lugano", "locarno", "mendrisio",
    "switzerland", "schweiz", "suisse", "svizzera",
}

COMMUTE_LOCATIONS = {
    "basel", "baden", "aarau", "kloten", "zurich", "zürich", "wallisellen",
    "rheinfelden", "frick", "brugg", "windisch",
}

MANDATORY_MARKERS = [
    "required", "erforderlich", "requis", "obligatoire", "obbligatorio",
    "muttersprache", "native", "natif", "madrelingua",
    "fluent", "courant", "couramment", "fließend", "fliessend", "fluente",
    "mandatory", "must have", "must be", "indispensable",
    "excellent", "ausgezeichnet",
    "bilingue", "bilingual", "zweisprachig",
    "c1", "c2", "muttersprachlich",
]

OPTIONAL_MARKERS = [
    "atout", "un plus", "ein plus", "is a plus", "is an asset",
    "von vorteil", "vorteilhaft", "preferred", "preferable", "preferably",
    "nice to have", "welcome", "willkommen", "bonus", "advantage",
    "vantaggio", "souhaité", "souhaitée", "wünschenswert",
    "additional languages", "any further language", "any other language",
    "weitere sprachen", "autres langues",
]

LANGUAGE_TOKENS_FR_IT = [
    "français", "francais", "french", "französisch",
    "italian", "italien", "italienisch", "italiano",
]

STOPWORDS_FR = {"le", "la", "les", "de", "du", "des", "et", "à", "au", "aux",
                "un", "une", "pour", "que", "qui", "dans", "vous", "nous",
                "votre", "notre", "avec", "sur", "par", "ce", "cette", "se"}
STOPWORDS_IT = {"il", "lo", "la", "i", "gli", "le", "di", "da", "del", "della",
                "e", "ed", "a", "ai", "un", "una", "per", "che", "con", "sul",
                "nel"}
STOPWORDS_DE = {"der", "die", "das", "den", "dem", "des", "und", "oder",
                "ein", "eine", "einen", "für", "mit", "zu", "von", "im", "in",
                "auf", "wir", "sie", "ihre", "unser", "unsere", "ist", "sind",
                "haben"}
STOPWORDS_EN = {"the", "and", "of", "to", "a", "in", "for", "is", "you",
                "your", "our", "we", "with", "on", "be", "as", "are",
                "this", "that", "have", "will", "or"}


# ============================================================================
# Individual filter functions
# ============================================================================

def is_swiss_location(location: str) -> bool:
    if not location:
        return False
    loc = location.lower().strip()
    if loc in SWISS_LOCATIONS:
        return True
    return any(ch in loc for ch in SWISS_LOCATIONS)


def is_commute_location(location: str) -> bool:
    if not location:
        return False
    loc = location.lower().strip()
    return any(c in loc for c in COMMUTE_LOCATIONS)


def _normalize_title(s: str) -> str:
    """Strip Swiss/French inclusive-language separators that break substring matching.
    'chef·fe de projets' -> 'cheffe de projets'
    'projektleiter:in' -> 'projektleiterin'
    """
    # Middle dot variations: ·, ⸱, ・, ·
    s = re.sub(r"[·⸱・]", "", s)
    # Colons used inclusively: 'er:in' -> 'erin'
    s = re.sub(r":(in|innen)\b", r"\1", s, flags=re.I)
    # Slash inclusive: 'er/in' -> 'erin' (but keep "and/or" untouched - simple heuristic)
    s = re.sub(r"/(in|innen)\b", r"\1", s, flags=re.I)
    return s


def passes_function_filter(title: str) -> tuple[bool, str]:
    """Reject installer/technician titles unless title also has a PM keyword."""
    t = _normalize_title(title).lower()
    if any(kw in t for kw in KEYWORDS_FUNCTION):
        return True, ""
    for bad in FUNCTION_REJECT_TITLE:
        if bad in t:
            return False, f"wrong function: '{bad}'"
    return True, ""


def passes_junior_filter(title: str) -> tuple[bool, str]:
    t = _normalize_title(title).lower()
    for bad in JUNIOR_REJECT:
        if re.search(rf"\b{re.escape(bad)}\b", t):
            return False, f"junior/intern: '{bad}'"
    return True, ""


def passes_tech_focus_title(title: str) -> tuple[bool, str]:
    t = _normalize_title(title).lower()
    for bad in TECH_REJECT:
        if bad in t:
            return False, f"non-PV/BESS tech in title: '{bad}'"
    return True, ""


def passes_workmode(workmode: str, location: str) -> tuple[bool, str]:
    wm = (workmode or "").lower()
    if "remote" in wm or "hybrid" in wm:
        return True, ""
    if not wm:
        return True, ""  # unknown -> flexible
    if "onsite" in wm or "on-site" in wm or "on site" in wm or "vor ort" in wm:
        if is_commute_location(location):
            return True, ""
        return False, f"onsite outside commute zone ({location})"
    return True, ""


def detect_jd_dominant_language(jd_body: str) -> str:
    if not jd_body or len(jd_body) < 100:
        return "unknown"
    tokens = re.findall(r"\b[a-zà-ÿ]+\b", jd_body.lower())
    if not tokens:
        return "unknown"
    counts = {
        "fr": sum(1 for t in tokens if t in STOPWORDS_FR),
        "it": sum(1 for t in tokens if t in STOPWORDS_IT),
        "de": sum(1 for t in tokens if t in STOPWORDS_DE),
        "en": sum(1 for t in tokens if t in STOPWORDS_EN),
    }
    sorted_counts = sorted(counts.items(), key=lambda x: -x[1])
    top, second = sorted_counts[0], sorted_counts[1]
    if top[1] == 0:
        return "unknown"
    if second[1] >= 0.4 * top[1]:
        return "mixed"
    return top[0]


def passes_language_filter(jd_body: str) -> tuple[bool, str]:
    if not jd_body:
        return True, ""
    dominant = detect_jd_dominant_language(jd_body)
    if dominant == "fr":
        return False, "JD body is dominantly French"
    if dominant == "it":
        return False, "JD body is dominantly Italian"
    body_lower = jd_body.lower()
    for lang_token in LANGUAGE_TOKENS_FR_IT:
        for m in re.finditer(rf"\b{re.escape(lang_token)}\b", body_lower):
            window = body_lower[max(0, m.start() - 150):m.end() + 150]
            has_mandatory = any(mk in window for mk in MANDATORY_MARKERS)
            has_optional = any(ok in window for ok in OPTIONAL_MARKERS)
            if has_mandatory and not has_optional:
                return False, f"{lang_token} stated as mandatory in JD"
    return True, ""


# ============================================================================
# JD fetcher for Adzuna jobs (employer page extraction)
# ============================================================================

UA = "Mozilla/5.0 (compatible; pv-job-alert/1.0)"
TIMEOUT = 12


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def fetch_jd_body(url: str) -> str:
    """Try to fetch and extract job description body from a URL.
    Returns empty string on any failure (caller treats empty as ambiguous = keep).
    """
    if not url:
        return ""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept-Language": "en;q=0.9, de;q=0.8, fr;q=0.7",
        })
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            html = r.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, Exception):
        return ""

    html = re.sub(r'<script.*?</script>', ' ', html, flags=re.S | re.I)
    html = re.sub(r'<style.*?</style>', ' ', html, flags=re.S | re.I)

    patterns = [
        r'<span[^>]*class="[^"]*jobdescription[^"]*"[^>]*>(.*?)</span>',
        r'<div[^>]*class="[^"]*jobdescription[^"]*"[^>]*>(.*?)</div>',
        r'<div[^>]*class="[^"]*job-description[^"]*"[^>]*>(.*?)</div>',
        r'<div[^>]*class="[^"]*description[^"]*"[^>]*>(.*?)</div>',
        r'<article[^>]*>(.*?)</article>',
        r'<main[^>]*>(.*?)</main>',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.S)
        if m:
            body = _strip_tags(m.group(1))
            if len(body) > 200:
                return body
    return ""


# ============================================================================
# Main entry: apply_filter_chain
# ============================================================================

def apply_filter_chain(
    title: str,
    location: str,
    jd_body: str = "",
    workmode: str = "",
    short_description: str = "",
    require_pm_keyword: bool = False,
) -> tuple[bool, str]:
    """Apply the full filter chain to a single job.

    Args:
        title: job title
        location: job location string
        jd_body: full JD text (from fetch_jd_body), empty string if unavailable
        workmode: 'hybrid'/'remote'/'onsite'/'' if unknown
        short_description: Adzuna's short description (used as JD fallback)
        require_pm_keyword: if True, title MUST contain a PM/procurement keyword.
            Use True for unfiltered employer scrapes (Swiss employer module).
            Use False for Adzuna where the API query already filtered by keyword.

    Returns:
        (keep, reject_reason). If keep=False, reject_reason is the cause.
    """
    if not is_swiss_location(location):
        return False, "not Swiss location"

    if require_pm_keyword:
        t = _normalize_title(title).lower()
        if not any(kw in t for kw in KEYWORDS_FUNCTION):
            return False, "no PM/procurement keyword in title"

    ok, reason = passes_function_filter(title)
    if not ok:
        return False, reason

    ok, reason = passes_junior_filter(title)
    if not ok:
        return False, reason

    ok, reason = passes_tech_focus_title(title)
    if not ok:
        return False, reason

    ok, reason = passes_workmode(workmode, location)
    if not ok:
        return False, reason

    # Language filter — use full JD if available, fall back to short description
    body_for_lang = jd_body or short_description
    ok, reason = passes_language_filter(body_for_lang)
    if not ok:
        return False, reason

    return True, ""
