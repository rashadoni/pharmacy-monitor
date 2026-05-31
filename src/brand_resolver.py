"""Recover the REAL brand of a product and decide when two brands genuinely conflict.

Why this exists
---------------
`products.brand` is polluted: `brand_catalog.extract_brand()` falls back to the
product's first name-word when the catalog misses, so ~92% of pharmonline / ~81%
of aptekonline rows store the GENERIC name ("Alaqanqal") instead of the firm
("Biola" / "Herba Flora"). The matcher then clusters different-brand commodities
(milk-thistle oil by Biola vs Herba Flora) into one match — the bug the client hit.

`brand_verified` (Product column) holds the brand recovered from an AUTHORITATIVE
source per site:
  - aloe        → the already-clean scraped `brand` field
  - aptekonline → the ``"brand":{"id":N,"name":"…"}`` JSON embedded in the product page
  - pharmonline → the brand token in the URL slug (page is a JS SPA; slug carries it)

The conflict guard (used by the matcher) blocks a cross-site match ONLY when BOTH
products have a confident **consumer** brand and they differ. Manufacturer COMPANIES
(Merck KGaA, Egis İlaç, Pharmex Rom Industry SRL, Nycomed …) are treated as
non-discriminating, so trade-name drugs keep matching across manufacturers
(Konkor by Merck == Konkor by Nycomed) — only commodity consumer brands split.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from src.normalize import strip_accents

# Above this fuzzy similarity, two differing brand strings are treated as the
# SAME brand spelled differently (transliteration / suffix), NOT a conflict:
# Borisov↔Borisovsky Zmp=100, Medizin↔Medizen=86, Nijfarm↔Nizhfarm=80 vs genuine
# Biola↔Herba Flora=50, Xerbes↔Herba Flora=60 (measured on prod, 2026-05-31).
_BRAND_SAME_FUZZ = 72

# ── manufacturer-company detection ──────────────────────────────────────────
# A brand reads as a manufacturer COMPANY (non-discriminating) if it carries a
# corporate suffix/keyword, or is a known suffix-less pharma manufacturer.
# Consumer brands (Biola, Herba Flora, Medoil, Fitooil) carry none of these.
_CORP_SUFFIX_RE = re.compile(
    r"(?<![a-z])("
    r"gmbh|kgaa|s\.?r\.?l|s\.?p\.?a|a\.?ş|pharmaceuticals?|pharma|"
    r"ila[çc]|holdings?|group|industr(?:y|ies|i)|sanayi|"
    r"laborator(?:y|ies)|labs?|chemicals?|nutraceuticals?|"
    r"kozmetik|cosmetics|medikal|healthcare|biotech|generics|remedies|"
    r"corporation|incorporated"
    r")(?![a-z])",
    re.IGNORECASE,
)
# Standalone corporate tokens — only count as "company" when they appear as a
# *separate* word (so they don't nuke real brands that merely contain them).
_CORP_WORDS = {
    "ag",
    "ltd",
    "llc",
    "inc",
    "co",
    "sa",
    "plc",
    "bv",
    "nv",
    "mmc",
    "ooo",
    "oao",
    "zao",
    "pao",
    "jsc",
    "corp",
    "ticaret",
    "tic",
    "as",
    "limited",
    "company",
    "lab",
    "labs",
    "pharm",
    "pharma",
    "ilac",
    "holding",
    "group",
    "industri",
    "sanayi",
    "manufacturing",
    "srl",
    "spa",
    "gmbh",
}
# Known suffix-less pharma manufacturers that must NOT read as a consumer brand
# (else trade-name drugs would falsely split). Extend as dry-runs reveal more.
_KNOWN_MANUFACTURERS = {
    "nycomed",
    "grindex",
    "biopharma",
    "aspar",
    "polens",
    "actavis",
    "sandoz",
    "krka",
    "egis",
    "richter",
    "gedeon",
    "stada",
    "teva",
    "mylan",
    "servier",
    "menarini",
    "abbott",
    "bayer",
    "pfizer",
    "roche",
    "novartis",
    "sanofi",
    "merck",
    "bilim",
    "sanovel",
    "atabay",
    "world medicine",
    "worldmedicine",
    "adamed",
    "polpharma",
    "recordati",
    "berlin chemie",
    "balkanpharma",
    "replekfarm",
    "farmak",
    "darnitsa",
    "arterium",
    "unifarm",
    "abdi ibrahim",
    "deva",
    "nobel",
    "mustafa nevzat",
    "drogsan",
    "fako",
    "vem",
    "kocak",
    "santa farma",
    "hexal",
    "ratiopharm",
    "zentiva",
    "biofarma",
    "embil",
    "neutec",
    "tripharma",
    "vega",
    "veqa",
    "alpen",
    "gricar",
    "pharmex",
    "fortex",
    "lek",
    "beiersdorf",
}


def _norm(s: str | None) -> str:
    """lower + strip-accents + collapse whitespace (ə→e, ş→s, ı→i, ç→c …)."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", strip_accents(s).lower().strip())


def is_manufacturer_company(brand: str | None) -> bool:
    """True if `brand` reads as a manufacturer COMPANY (non-discriminating)."""
    if not brand:
        return False
    n = _norm(brand)
    if not n:
        return False
    if _CORP_SUFFIX_RE.search(n):
        return True
    if n in _KNOWN_MANUFACTURERS:
        return True
    toks = n.split()
    if len(toks) >= 2 and toks[-1] in _CORP_WORDS:
        return True
    return False


def consumer_brand(brand_verified: str | None) -> str | None:
    """Normalized CONSUMER brand for the conflict guard.

    Returns None if the brand is unknown OR a manufacturer company (both treated
    as non-discriminating → the guard will not fire and recall is preserved).
    """
    if not brand_verified:
        return None
    if is_manufacturer_company(brand_verified):
        return None
    n = _norm(brand_verified)
    return n or None


def brands_conflict(a_verified: str | None, b_verified: str | None) -> bool:
    """True iff both products have a confident consumer brand that genuinely differs.

    Two differing strings that are fuzzy-similar (≥ _BRAND_SAME_FUZZ) are treated
    as the SAME brand spelled differently (Borisov / Borisovsky Zmp, Medizin /
    Medizen) — not a conflict. This catches the manufacturer-transliteration
    confound generically, without per-firm lists."""
    ca = consumer_brand(a_verified)
    cb = consumer_brand(b_verified)
    if not ca or not cb:
        return False
    if ca == cb:
        return False
    sim = max(fuzz.ratio(ca, cb), fuzz.partial_ratio(ca, cb), fuzz.token_set_ratio(ca, cb))
    return sim < _BRAND_SAME_FUZZ


# ── commodity gate ──────────────────────────────────────────────────────────
# The brand-conflict guard is ONLY safe for COMMODITY products — herbal/natural
# goods with a generic descriptive name (<plant> <form>) where the brand IS the
# product identity (Biola vs Herba Flora milk-thistle oil). For trade-name DRUGS
# the brand_verified field is the MANUFACTURER, which aloe and pharmonline spell
# differently (Nijfarm/Nizhfarm, Merk/Merck Sante, Berinqer/Boehringer) — guarding
# those would split ~115 correct matches (measured on prod). So we gate on the
# presence of a botanical form-word in the name. Markers are accent-normalized
# (ə→e, ş→s, ı→i, ç→c, ğ→g, ö→o, ü→u — see _norm).
_COMMODITY_MARKERS = {
    "yagi", "yag",  # oil  (yağı/yağ)
    "toxumu", "toxum",  # seed
    "cayi", "cay",  # tea  (çayı/çay)
    "otu",  # herb  (otu) — bare "ot" excluded (too common/short)
    "qabigi", "qabig",  # bark  (qabığı)
    "ekstrakti", "ekstrakt",  # extract
    "siresi",  # juice/sap  (şirəsi)
    "meyveleri", "meyve",  # fruit/berries  (meyvələri)
    "koku",  # root  (kökü)
    "yarpagi",  # leaf  (yarpağı)
    "covheri", "covher",  # tincture  (cövhəri)
    "gulu",  # flower  (gülü)
}


def is_commodity_name(name: str | None) -> bool:
    """True if the name carries a botanical/commodity form-word (oil/seed/tea/
    bark/extract/…) → a generic-named good where brand = identity. Used to gate
    the brand-conflict guard so it never fires on trade-name drugs."""
    if not name:
        return False
    return any(tok in _COMMODITY_MARKERS for tok in _norm(name).split())


# ── pharmonline slug → brand ────────────────────────────────────────────────
# pharmonline slugs look like `<name…>-<dosage>-<pack>-<brand…>-<country>`, e.g.
# `alaqanqal-yaghi-100-ml-biola-azerbaycan` → brand "biola". The brand is the
# alpha token-run just before the (optional) country suffix; we strip dosage/pack
# tokens, countries, generic form-words and corporate noise so only a real
# consumer-brand candidate survives.
_COUNTRIES = {
    "azerbaycan",
    "azerbaycani",
    "turkiye",
    "rusiya",
    "almaniya",
    "fransa",
    "italiya",
    "ispaniya",
    "polsa",
    "polsha",
    "ukrayna",
    "isvecre",
    "niderland",
    "hollandiya",
    "belcika",
    "avstriya",
    "cexiya",
    "macaristan",
    "yaponiya",
    "cin",
    "chin",
    "hindistan",
    "kanada",
    "abs",
    "ingiltere",
    "koreya",
    "misir",
    "iran",
    "sloveniya",
    "slovakiya",
    "bolqaristan",
    "gurcustan",
    "litva",
    "litvaniya",
    "latviya",
    "estoniya",
    "isvec",
    "finlandiya",
    "danimarka",
    "norvec",
    "portuqaliya",
    "yunanistan",
    "xorvatiya",
    "serbiya",
    "tayland",
    "vyetnam",
    "pakistan",
    "israil",
    "beae",
    "uae",
    "ruminiya",
    "rumuniya",
    "belarus",
    "boyuk",
    "britaniya",
    "san",
    "marino",
    "bosniya",
    "banqladesh",
    "cenubi",
    "afrika",
    "diger",
    "grfg",
    "indoneziya",
    "malayziya",
    "vietnam",
}
# Slug tokens that are NOT brands (forms, packaging, descriptors, corporate
# noise). `is_brand_blacklisted` already covers many generic words; this catches
# the slug-specific leakers seen in real data.
_SLUG_NOISE = {
    "yagh",
    "yag",
    "yaghi",
    "yaghli",
    "mehlul",
    "mehlulu",
    "sherbet",
    "sirop",
    "siropu",
    "sprey",
    "spray",
    "krem",
    "kremi",
    "gel",
    "geli",
    "damci",
    "damcilari",
    "damcisi",
    "tablet",
    "tableti",
    "kapsul",
    "kapsula",
    "kapsulalar",
    "kapsulasi",
    "ampula",
    "ampul",
    "shamlar",
    "shamcasi",
    "paket",
    "paketi",
    "eded",
    "ededi",
    "suspenziya",
    "drops",
    "pastasi",
    "qr",
    "gr",
    "mg",
    "mq",
    "ml",
    "kg",
    "ed",
    "iu",
    "sm",
    "cm",
    "n",
    "g",
    "tea",
    "chay",
    "cay",
    "cayi",
    "toxum",
    "toxumu",
    "otu",
    "meyveleri",
    "co",
    "ltd",
    "srl",
    "mmc",
    "as",
    "group",
    "holding",
    "pharma",
    "ilac",
    "ilach",
    "kozmetik",
    "cosmetics",
    "lab",
    "labs",
    "nutraceuticals",
    "heb",
    "drage",
    "burun",
    "goz",
    "qulaq",
    "dish",
    "agiz",
    "vasiteleri",
    "mehsullari",
    "mehsul",
    "maddeler",
    "balzam",
    "sampun",
    "sabun",
}
_DOSE_RE = re.compile(r"\d")


def brand_from_pharmonline_slug(url: str | None) -> str | None:
    """Extract a consumer-brand candidate from a pharmonline product URL slug.

    Returns the raw brand string (Title-cased, possibly 2 words) or None. The
    result is still passed through `consumer_brand()`/`brands_conflict()` at match
    time, so corporate/noise leftovers stay non-discriminating.
    """
    if not url:
        return None
    slug = url.rstrip("/").split("/")[-1].lower()
    toks = slug.split("-")
    if len(toks) < 2:
        return None
    # drop a trailing country suffix (may be 2 words: "boyuk britaniya")
    while toks and toks[-1] in _COUNTRIES:
        toks.pop()
    if not toks:
        return None
    brand: list[str] = []
    for t in reversed(toks):
        if not t or len(t) < 3:
            break
        if _DOSE_RE.search(t):
            break
        if t in _COUNTRIES or t in _SLUG_NOISE:
            break
        if is_brand_blacklisted(t):
            break
        brand.insert(0, t)
        if len(brand) >= 2:
            break
    if not brand:
        return None
    return " ".join(w.title() for w in brand)


# imported late to avoid a circular import at module load
from src.brand_catalog import is_brand_blacklisted  # noqa: E402
