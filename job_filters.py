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
    "buyer", "einkauf", "einkäufer", "achats", "acheteur", "bauherrenvertretung", "bauherrenberatung", "bauherrenvertreter",
    "eigentümervertretung", "eigentumervertretung",
    "technische due diligence", "technical due diligence",
    "gesamtprojektleitung", "gesamtprojektleiter",
    "inbetriebnahme",  # commissioning-lead roles
]

TECH_REJECT = [
    "hydro", "wasserkraft", "hydroélectri", "hydropower", "hydraulique",
    "pumped storage",
    "nuclear", "kernkraft", "nucléaire",
    "gas-fired", "gas turbine", "lng", "lpg",
    "thermal", "thermoélectri", "kohle", "coal",
    "hvac", "cvc", "chauffage", "klima", "klimat", "heating",
    "sanitär", "sanitaire",
]

# --- Wind: reject wind-ONLY titles; keep PV / PV+BESS hybrid portfolios ---
# "Projektleiter Windenergie" -> reject. "Projektleiter Solar & Wind" -> keep
# (PV token present). Decision 2026-07-06.
WIND_TOKENS = ["wind", "windkraft", "windenergie", "éolien", "eolien"]
# --- Nuclear: Swiss postings rarely say "Kernkraft" in the title. Reject on
# title OR body context, unless the title carries a PV/BESS token. ---
NUCLEAR_TOKENS = [
    "nuclear", "nuklear", "kernkraft", "kernenergie", "kernanlage",
    "nucléaire", "nucleaire", "atomkraft",
    "reaktor", "reactor", "brennelement", "brennstab",
    "rückbau", "rueckbau", "stilllegung",  # decommissioning
    "leibstadt", "beznau", "gösgen", "goesgen", "mühleberg", "muehleberg",
]
# Short acronyms need word boundaries (substring would hit e.g. "akkwirtschaft")
NUCLEAR_TOKENS_RE = [r"\bkkw\b", r"\bakw\b"]

PV_TOKENS = [
    "pv", "photovolta", "solar", "bess", "batterie", "battery",
    "speicher", "storage", "energiespeicher",
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
    "chef de chantier", "bauleiter elektro", "bauleiter installation", "aussendienst", "projektentwickler",
]

# --- Trade-track qualification required, found in JD BODY (not title) ---
# These gate the role behind the Swiss electrical vocational certificate.
# Scanned in the body because titles are clean PM titles ("Projektleiter PV").
# NOTE: matched as independent tokens, NOT the old exact phrase "elektroinstallateur efz".
REQUIREMENT_REJECT_BODY = [
    r"\befz\b",
    r"elektroinstallateur",
    r"elektroplaner",
    r"eidg\.?\s*fachausweis",
    r"eidgenössischer fachausweis",
    r"sicherheitsexperte",
    r"sicherheitsberater",
    r"abgeschlossene\s+(grund)?ausbildung\s+(als\s+|im\s+)?elektro",
]

# --- Bauleitung as a core listed duty (site-construction management) ---
# Matches only ownership/execution phrasings. Mere mention (e.g. owner-side
# "Koordination der Bauleitung" / "Schnittstelle zur Bauleitung") must NOT
# reject — that phrasing describes OE work, not a Bauleiter role.
BAULEITUNG_REJECT_BODY = [
    r"(übernahme|übernehmen|verantwortlich für|verantwortung für|führung|leitung)\s+(der|die|einer)?\s*bauleitung",
    r"\bals bauleiter(in)?\b",
    r"\bbauleitung vor ort\b",
    r"baustellen(leitung|verantwortung)",
]

# --- Sales/acquisition as primary function (segment-lead/Vertrieb roles) ---
SALES_REJECT_BODY = [
    r"auftragsakquise",
    r"kundenakquise",
    r"\bakquise\b",
    r"neukundengewinnung",
    r"vertriebsverantwortung",
]

# --- Explicit German language walls (criteria v9, 2026-07-21): only C1/C2, stilsicher, fliessend, Muttersprache as requirement. Unstated German = pre-screen at scoring stage, NOT filtered here. ---
GERMAN_WALL_REJECT_BODY = [r"stilsicher\w*\s+deutsch", r"muttersprache\s*:?\s*deutsch", r"deutsch\s+als\s+muttersprache", r"deutsch\w*\s+(?:auf\s+)?(?:niveau\s+)?c[12]\b", r"\bc[12]\b[\s-]*(?:niveau\s+)?deutsch", r"flie(?:ss|ß)end\w*\s+deutsch", r"deutsch\w*\s+flie(?:ss|ß)end"]

# --- Aussendienst + driving licence (criteria 2026-07-21, no Kat. B): reject only when both tokens appear within 200 chars; licence-only mentions stay in scope (rail-reachable). ---
FIELD_LICENCE_REJECT_BODY = [r"aussendienst.{0,200}f(?:ü|ue)hrer(?:schein|ausweis)", r"f(?:ü|ue)hrer(?:schein|ausweis).{0,200}aussendienst"]

# --- Agency-shell signals: staffing reposts for undisclosed end clients ---
AGENCY_POSTER_NAMES = [
    "dasteam", "das team", "addexpert", "excellent go4", "excellent1",
    "zürcher consulting", "zuercher consulting",
]
AGENCY_SHELL_PHRASES = [
    r"für unsere[nr]?\s+kund",
    r"\bunser kunde\b",
    r"personalverleih", r"personalvermittlung", r"verleih von personal",
]

# --- Target-region restriction: German-speaking Switzerland (Deutschschweiz) ---
# Standing default per 2026-07-06 decision: keep all Deutschschweiz locations;
# reject Romandie (FR) and Ticino (IT) only. Onsite-commutability is enforced
# separately by passes_workmode (Basel/Aargau/Zürich commute zone) — a Luzern
# onsite role still fails workmode; a Luzern hybrid role now passes.
# Biel/Bienne is bilingual and deliberately NOT rejected.
TARGET_LOCATION_REJECT = [
    # Romandie
    "genève", "geneve", "geneva", "genf", "lausanne", "fribourg",
    "neuchâtel", "neuchatel", "neuenburg", "sion", "sitten",
    "martigny", "monthey", "yverdon", "delémont", "delemont",
    "morges", "vevey", "montreux", "nyon", "renens",
    "granges-paccot", "boudry", "guin", "boudevilliers", "epagny",
    "payerne", "sâles", "château-d'oex",
    # Ticino
    "lugano", "locarno", "bellinzona", "mendrisio",
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
    # Commute-zone towns (were missing -> failed "not Swiss location" before
    # workmode could ever evaluate them) + common Deutschschweiz towns/cantons
    "rheinfelden", "frick", "brugg", "windisch",
    "zofingen", "egerkingen", "hünenberg", "huenenberg", "wettingen",
    "dietikon", "spreitenbach", "pratteln", "muttenz", "münchenstein",
    "liestal", "allschwil", "reinach", "dübendorf", "duebendorf",
    "opfikon", "regensdorf", "schlieren", "uster", "wil",
    "aargau", "basel-landschaft", "basel-stadt", "baselland",
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
    # Word-boundary match, not substring: prevents "wil"->"Wilhelmshaven",
    # "zug"->"Zugspitze"-class false positives while keeping "basel-stadt",
    # "greater zurich area", "kanton aargau" matches.
    return any(re.search(rf"(?<![a-zà-ÿ]){re.escape(ch)}(?![a-zà-ÿ])", loc)
               for ch in SWISS_LOCATIONS)


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
    # Wind-only rule: wind token present AND no PV/BESS token -> reject.
    # Hybrid portfolios (Solar & Wind, PV/Wind/Speicher) are kept.
    if any(w in t for w in WIND_TOKENS) and not any(p in t for p in PV_TOKENS):
        return False, "wind-only role (no PV/BESS in title)"
    # Nuclear in title (PV token overrides — e.g. "PV auf KKW-Areal" edge case)
    if _nuclear_hit(t) and not any(p in t for p in PV_TOKENS):
        return False, "nuclear role (title)"
    return True, ""


def _nuclear_hit(text: str) -> bool:
    if any(tok in text for tok in NUCLEAR_TOKENS):
        return True
    return any(re.search(p, text) for p in NUCLEAR_TOKENS_RE)


def passes_tech_focus_body(title: str, jd_body: str, short_description: str = "") -> tuple[bool, str]:
    """Nuclear roles hide behind generic titles ("Projektleiter Grossprojekte").
    If the title has no PV/BESS token, scan body + short description for
    nuclear context. A PV token in the title always keeps (employer boilerplate
    mentioning the nuclear fleet must not kill PV roles at Axpo/Alpiq)."""
    t = _normalize_title(title).lower()
    if any(p in t for p in PV_TOKENS):
        return True, ""
    body = ((jd_body or "") + " " + (short_description or "")).lower()
    if not body.strip():
        return True, ""
    if _nuclear_hit(body):
        return False, "nuclear role (JD body context, no PV in title)"
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


def _http_get(url: str) -> str:
    """GET with one retry. Returns raw HTML or empty string."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "en;q=0.9, de;q=0.8, fr;q=0.7",
    })
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception:
            if attempt == 2:
                return ""
    return ""


def fetch_jd_body(url: str) -> str:
    """Fetch and extract job description body from a URL.
    Extraction order (most reliable first):
      1. schema.org JobPosting JSON-LD "description" (most job portals embed this,
         including JS-rendered ones — the JSON-LD is server-side)
      2. Known description-container patterns
      3. og:description / meta description (better than nothing for tech-focus scan)
      4. Whole-page text fallback if substantial
    Returns "" only when nothing usable was extracted; callers should LOG that.
    """
    if not url:
        return ""
    html = _http_get(url)
    if not html:
        return ""

    # 1) JSON-LD JobPosting — search BEFORE stripping <script> tags
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.S | re.I,
    ):
        raw = m.group(1).strip()
        try:
            import json as _json
            data = _json.loads(raw)
        except Exception:
            continue
        objs = data if isinstance(data, list) else [data]
        for obj in objs:
            if not isinstance(obj, dict):
                continue
            graph = obj.get("@graph")
            if isinstance(graph, list):
                objs.extend(o for o in graph if isinstance(o, dict))
                continue
            if obj.get("@type") in ("JobPosting", ["JobPosting"]):
                desc = obj.get("description", "")
                if desc:
                    body = _strip_tags(desc)
                    if len(body) > 80:
                        return body

    stripped = re.sub(r'<script.*?</script>', ' ', html, flags=re.S | re.I)
    stripped = re.sub(r'<style.*?</style>', ' ', stripped, flags=re.S | re.I)

    # 2) Known container patterns
    patterns = [
        r'<span[^>]*class="[^"]*jobdescription[^"]*"[^>]*>(.*?)</span>',
        r'<div[^>]*class="[^"]*jobdescription[^"]*"[^>]*>(.*?)</div>',
        r'<div[^>]*class="[^"]*job-description[^"]*"[^>]*>(.*?)</div>',
        r'<div[^>]*class="[^"]*description[^"]*"[^>]*>(.*?)</div>',
        r'<article[^>]*>(.*?)</article>',
        r'<main[^>]*>(.*?)</main>',
    ]
    for pat in patterns:
        m = re.search(pat, stripped, re.S)
        if m:
            body = _strip_tags(m.group(1))
            if len(body) > 200:
                return body

    # 3) Meta descriptions — short, but enough for token-based tech-focus scan
    for meta_pat in [
        r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\'](.*?)["\']',
        r'<meta[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']',
    ]:
        m = re.search(meta_pat, html, re.S | re.I)
        if m:
            body = _strip_tags(m.group(1))
            if len(body) > 80:
                return body

    # 4) Whole-page fallback
    body = _strip_tags(stripped)
    return body if len(body) > 500 else ""


def _matches_any(patterns, text):
    return any(re.search(p, text) for p in patterns)


def passes_requirements_body(jd_body: str) -> tuple[bool, str]:
    """Scan JD body for trade-track gate / Bauleitung / sales-primary.
    Fails OPEN on empty body (no body fetched -> let scoring decide)."""
    body = (jd_body or "").lower()
    if not body:
        return True, ""
    if _matches_any(REQUIREMENT_REJECT_BODY, body):
        return False, "EFZ / eidg. Fachausweis Elektro required (trade-track gate)"
    if _matches_any(BAULEITUNG_REJECT_BODY, body):
        return False, "Bauleitung as core duty"
    if _matches_any(SALES_REJECT_BODY, body):
        return False, "sales/acquisition primary function"
    if _matches_any(GERMAN_WALL_REJECT_BODY, body):
        return False, "explicit German language wall (C1/stilsicher/Muttersprache)"
    if _matches_any(FIELD_LICENCE_REJECT_BODY, body):
        return False, "Aussendienst + driving licence required (no Kat. B)"
    return True, ""


def passes_agency_shell(company: str, jd_body: str) -> tuple[bool, str]:
    """Reject staffing-agency reposts for undisclosed end clients."""
    comp = (company or "").lower()
    body = (jd_body or "").lower()
    if any(name in comp for name in AGENCY_POSTER_NAMES):
        return False, f"staffing agency poster ({comp})"
    if body and _matches_any(AGENCY_SHELL_PHRASES, body):
        return False, "agency shell: undisclosed end client"
    return True, ""


def passes_target_location(location: str) -> tuple[bool, str]:
    """Restrict to German-speaking Switzerland; reject Romandie/Ticino locations."""
    loc = (location or "").lower()
    if not loc:
        return True, ""
    if any(bad in loc for bad in TARGET_LOCATION_REJECT):
        return False, f"outside Deutschschweiz target region ({location})"
    return True, ""


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
    company: str = "",
    require_body: bool = False,
) -> tuple[bool, str]:
    """Apply the full filter chain to a single job.

    Args:
        title: job title
        location: job location string
        jd_body: full JD text (from fetch_jd_body), empty string if unavailable
        workmode: 'hybrid'/'remote'/'onsite'/'' if unknown
        short_description: Adzuna's short description (used as JD fallback)
        require_body: if True, a job whose title has no PV/BESS token AND whose
            jd_body and short_description are both empty is rejected (fail-closed).
            Use True for employer-page scrapes; False for Adzuna (always has a
            short description).
        require_pm_keyword: if True, title MUST contain a PM/procurement keyword.
            Use True for unfiltered employer scrapes (Swiss employer module).
            Use False for Adzuna where the API query already filtered by keyword.

    Returns:
        (keep, reject_reason). If keep=False, reject_reason is the cause.
    """
    if not is_swiss_location(location):
        return False, "not Swiss location"

    # Target-region restriction (Deutschschweiz; Romandie/Ticino rejected)
    ok, reason = passes_target_location(location)
    if not ok:
        return False, reason

    # Agency-shell reject (uses company name + body phrasing)
    ok, reason = passes_agency_shell(company, jd_body)
    if not ok:
        return False, reason

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

    ok, reason = passes_tech_focus_body(title, jd_body, short_description)
    if not ok:
        return False, reason

    # Strict mode: generic title (no PV/BESS token) with NO body text at all
    # cannot be tech-verified — reject instead of failing open. Titles carrying
    # a PV token stay exempt: they are self-identifying.
    if require_body and not (jd_body or short_description):
        t = _normalize_title(title).lower()
        if not any(p in t for p in PV_TOKENS):
            return False, "JD body unretrievable and title not self-identifying (strict)"

    ok, reason = passes_workmode(workmode, location)
    if not ok:
        return False, reason

    # Language filter — use full JD if available, fall back to short description
    body_for_lang = jd_body or short_description
    ok, reason = passes_language_filter(body_for_lang)
    if not ok:
        return False, reason

    # Requirements-body filter — EFZ/Fachausweis gate, Bauleitung, sales-primary
    ok, reason = passes_requirements_body(jd_body)
    if not ok:
        return False, reason

    return True, ""
