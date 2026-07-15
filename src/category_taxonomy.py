"""Compact, source-independent category taxonomy for comparison analytics.

Source catalogues remain intentionally untouched: their categories are useful
for scraping, but they are too granular and structurally incompatible for a
business comparison screen.  This module maps only semantically clear source
categories into a small canonical top level.  Ambiguous buckets such as
``şamlar`` (suppositories), ``inyeksiyalar`` and Aloe's broad ``dermanlar`` are
left unmapped instead of becoming misleading dashboard categories.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from src.normalize import strip_accents


@dataclass(frozen=True)
class CanonicalCategory:
    key: str
    label_ru: str
    label_az: str


CANONICAL_CATEGORIES: tuple[CanonicalCategory, ...] = (
    CanonicalCategory("infectious_immune", "Инфекции и иммунитет", "İnfeksiyalar və immunitet"),
    CanonicalCategory("respiratory_ent", "Дыхательная система и ЛОР", "Tənəffüs sistemi və LOR"),
    CanonicalCategory("cardiovascular_blood", "Сердце, сосуды и кровь", "Ürək, damarlar və qan"),
    CanonicalCategory("nervous_system", "Нервная система", "Sinir sistemi"),
    CanonicalCategory("digestive_system", "Пищеварительная система", "Həzm sistemi"),
    CanonicalCategory(
        "urogenital_reproductive",
        "Мочеполовая и репродуктивная система",
        "Sidik-cinsiyyət və reproduktiv sağlamlıq",
    ),
    CanonicalCategory(
        "endocrine_metabolism",
        "Эндокринология и обмен веществ",
        "Endokrinologiya və maddələr mübadiləsi",
    ),
    CanonicalCategory(
        "pain_musculoskeletal",
        "Боль и опорно-двигательная система",
        "Ağrı və dayaq-hərəkət sistemi",
    ),
    CanonicalCategory("dermatology", "Дерматология", "Dermatologiya"),
    CanonicalCategory("eye_health", "Зрение и здоровье глаз", "Göz sağlamlığı"),
    CanonicalCategory("oral_care", "Полость рта и зубы", "Ağız boşluğu və dişlər"),
    CanonicalCategory(
        "vitamins_supplements",
        "Витамины, БАД и натуральные средства",
        "Vitaminlər, BFƏ və təbii vasitələr",
    ),
    CanonicalCategory("mother_baby", "Мама и ребёнок", "Ana və uşaq"),
    CanonicalCategory(
        "personal_care", "Уход, гигиена и косметика", "Qulluq, gigiyena və kosmetika"
    ),
    CanonicalCategory(
        "medical_devices", "Медицинские изделия и техника", "Tibbi vasitələr və avadanlıq"
    ),
    CanonicalCategory("oncology", "Онкология", "Onkologiya"),
    CanonicalCategory("homeopathy", "Гомеопатия", "Homeopatiya"),
)

_BY_KEY = {category.key: category for category in CANONICAL_CATEGORIES}

# Ordered from narrow/specific to broader retail concepts.  Matching is based
# on normalized source slug + labels, never on product names.
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("oncology", ("onkoloji", "onkolog", "sis xestelik", "tumor")),
    ("homeopathy", ("homeopatik", "homeopati", "homeopath")),
    (
        "oral_care",
        (
            "agiz boslug",
            "dish mecun",
            "dis mecun",
            "dish firca",
            "dis firca",
            "dental",
            "stomatolog",
        ),
    ),
    (
        "eye_health",
        ("goz xest", "quru goz", "oftalm", "qlaukoma", "katarakta", "gorme zeif", "eye care"),
    ),
    (
        "mother_baby",
        (
            "ana ve usaq",
            "ana ve ushaq",
            "usaq",
            "ushaq",
            "hamile",
            "laktasiya",
            "dogus",
            "korp",
            "baby",
            "pediatr",
        ),
    ),
    (
        "medical_devices",
        (
            "tibbi avadan",
            "tibbi vasite",
            "tibbi geyim",
            "tonometr",
            "termometr",
            "qlukometr",
            "inhaliyator",
            "pulsoximetr",
            "aparat",
            "sarghi material",
            "kateter",
            "zond",
            "spris",
            "siring",
            "tibbi mask",
            "respirator",
            "tibbi elcek",
            "optik",
            "kontakt linza",
            "cerrahi vasite",
            "test ucun vasite",
        ),
    ),
    (
        "dermatology",
        (
            "deri xest",
            "dermatit",
            "ekzema",
            "psoria",
            "sizanaq",
            "demrov",
            "deri infeksi",
            "dirnaq xest",
            "sac xest",
            "yaniq zamani",
            "skin disease",
        ),
    ),
    (
        "personal_care",
        (
            "deriye qulluq",
            "derisine qulluq",
            "uze qulluq",
            "bedene qulluq",
            "saca qulluq",
            "sach uchun",
            "sac ucun",
            "shampun",
            "balzam",
            "kosmetik",
            "gigiyen",
            "intim gel",
            "dus gel",
            "sabun",
            "epilyasiya",
            "depilyasiya",
            "ter eleyhine",
            "dezodor",
            "etir",
            "parfum",
            "qulluq vasite",
        ),
    ),
    (
        "vitamins_supplements",
        (
            "vitamin",
            "mineral",
            "tebii vasite",
            "bitki yagi",
            "bitki yag",
            "bitki cay",
            "derman bitki",
            "baliq yagi",
            "qida elave",
            "supplement",
            "bad",
        ),
    ),
    (
        "endocrine_metabolism",
        (
            "endokrin",
            "diabet",
            "qalxanabenzer",
            "metabol",
            "maddeler mubadile",
            "piylenme",
            "beden cekisi",
            "qidalanma poz",
            "dieta",
        ),
    ),
    (
        "digestive_system",
        (
            "hezm",
            "mede",
            "bagirsaq",
            "qaraciyer",
            "qara ciyer",
            "od xest",
            "qebizlik",
            "ishal",
            "ishalda",
            "qusma",
            "urekbulanma",
            "kop eleyhine",
            "qastrit",
            "kolit",
            "pankreas",
            "mədə",
        ),
    ),
    (
        "urogenital_reproductive",
        (
            "sidik",
            "boyrek",
            "prostat",
            "qadin sag",
            "qadin xest",
            "ginekoloji",
            "ginekolog",
            "menstru",
            "klimaks",
            "menopauz",
            "sonsuzluq",
            "potensiya",
            "kontrasept",
            "cinsi yolla",
            "mastopatiya",
        ),
    ),
    (
        "cardiovascular_blood",
        (
            "urek damar",
            "qan dovrani",
            "hipertoni",
            "antihipertenz",
            "aritmi",
            "stenokard",
            "trombo",
            "tromboflebit",
            "ateroskleroz",
            "varikoz",
            "urek catish",
            "urek chatish",
            "qanaxma",
            "anemiya",
            "qan xest",
            "miokard",
            "insult",
        ),
    ),
    (
        "nervous_system",
        (
            "sinir sistemi",
            "yuxu poz",
            "depress",
            "epilep",
            "sizofren",
            "parkinson",
            "migren",
            "neyropati",
            "nevral",
            "beyin fealiyy",
            "beyin qan",
            "sakitlesh",
            "sakitles",
            "asten",
            "yorğunluq",
            "yorgunluq",
        ),
    ),
    (
        "pain_musculoskeletal",
        (
            "agrikesici",
            "agri zamani",
            "agri eleyhine",
            "sumuk ezele",
            "oynaq",
            "artrit",
            "artroz",
            "xondro",
            "osteoporoz",
            "raxit",
            "podaqra",
            "ortoped",
            "miorelaksant",
            "spazmolitik",
            "travmatik agri",
            "travma",
            "dayaq hereket",
            "pain",
        ),
    ),
    (
        "respiratory_ent",
        (
            "teneffus",
            "burun bogaz qulaq",
            "burun",
            "bogaz",
            "qulaq xest",
            "oskurek",
            "qrip",
            "bronxit",
            "pnevmon",
            "astma",
            "rinit",
            "sinusit",
            "faringit",
            "laringit",
            "angina",
            "respirator distress",
        ),
    ),
    (
        "infectious_immune",
        (
            "bakterial",
            "virus infeksi",
            "gobelek infeksi",
            "antiparazitar",
            "parazitar",
            "antihelmint",
            "qurd xest",
            "infeksion",
            "immunitet",
            "immuncatish",
            "allergik reaksi",
            "anafilakt",
        ),
    ),
)

_SPACE_RE = re.compile(r"[^a-z0-9а-яё]+", re.IGNORECASE)


def _normalized_text(*values: str | None) -> str:
    combined = " ".join(value for value in values if value)
    return " ".join(_SPACE_RE.sub(" ", strip_accents(combined).casefold()).split())


def canonical_category(key: str) -> CanonicalCategory | None:
    return _BY_KEY.get(key)


def classify_source_category(
    site: str,
    slug: str | None,
    *,
    label_ru: str | None = None,
    label_az: str | None = None,
) -> CanonicalCategory | None:
    """Map one clear source category to the compact canonical taxonomy.

    ``site`` is accepted deliberately even though the first rules are shared:
    numeric AptekOnline slugs require a label, while broad Aloe buckets are
    explicitly non-voting.  Unknown or format-only source categories return
    ``None`` and therefore never create dashboard noise.
    """
    if not slug:
        return None
    normalized_slug = _normalized_text(slug)
    if site == "aloe" and normalized_slug in {"dermanlar", "tibbi vasiteler"}:
        return None
    if normalized_slug in {
        "inyeksiyalar",
        "inyeksiyalar 8",
        "shamlar",
        "shamlar 2",
        "shamlar 3",
        "shamlar 4",
        "xarici vasiteler",
        "xarici vasiteler 3",
        "xarici vasiteler ve inyeksiyalar",
        "yerli vasiteler",
        "yerli vasiteler 2",
        "yerli vasiteler 3",
        "yerli vasiteler 4",
        "yerli vasiteler 5",
    }:
        return None

    text = _normalized_text(slug, label_ru, label_az)
    for key, signals in _RULES:
        if any(signal in text for signal in signals):
            return _BY_KEY[key]
    return None


def source_category_labels(session: Any) -> dict[tuple[str, str], tuple[str | None, str | None]]:
    """Return deterministic labels for every site/slug represented in Category.

    Production legitimately contains duplicate source slugs.  Prefer the
    source-native row, then an active row, then the newest id, matching the API
    label-selection contract.
    """
    from src.storage import Category

    attrs = {
        "pharmonline": "pharmonline_slug",
        "aptekonline": "aptekonline_slug",
        "aloe": "aloe_slug",
    }
    prefixes = {"pharmonline": "pharma", "aptekonline": "aptek", "aloe": "aloe"}
    chosen: dict[tuple[str, str], tuple[tuple[bool, bool, int], tuple[str | None, str | None]]] = {}
    for row in session.scalars(select(Category)).all():
        for site, attr in attrs.items():
            slug = getattr(row, attr)
            if not slug:
                continue
            priority = (
                row.key == f"{prefixes[site]}_{slug}",
                bool(row.is_active),
                int(row.id or 0),
            )
            map_key = (site, str(slug))
            current = chosen.get(map_key)
            if current is None or priority > current[0]:
                chosen[map_key] = (priority, (row.label_ru, row.label_az))
    return {key: value for key, (_priority, value) in chosen.items()}
