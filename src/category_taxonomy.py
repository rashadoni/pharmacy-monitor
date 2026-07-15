"""Compact, source-independent category taxonomy for comparison analytics.

Source catalogues remain intentionally untouched: their categories are useful
for scraping, but they are too granular and structurally incompatible for a
business comparison screen.  This module maps only semantically clear source
categories into a small canonical top level.  Ambiguous buckets such as
``şamlar`` (suppositories), ``inyeksiyalar`` and Aloe's broad ``dermanlar`` are
left unmapped instead of becoming misleading dashboard categories.

Design contract (do not regress):

* **Order independence.** Rules are collected, then resolved. Reordering
  ``_RULES`` must never change a result. First-match-wins previously let a
  generic ``respirator`` device signal swallow the ``respirator distress``
  disease bucket purely because it was listed earlier.
* **Fail closed.** When two canonical groups both fire and no explicit
  dominance rule applies, the category stays unmapped and is reported as
  ambiguous. A plausible-but-unproven label is worse than no row.
* **One spelling.** PharmOnline slugs transliterate with digraphs
  (``şampunlar`` → ``shampunlar``) while AptekOnline labels carry diacritics
  (``Şampunlar``). ``strip_accents`` folds ``ş``→``s`` but leaves ``sh`` alone,
  so both are folded to one canonical form before matching. Otherwise the same
  concept classifies on one site and not the other.
* **Per-field matching.** Slug and labels are matched separately so a phrase
  can never span the boundary between two unrelated fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from sqlalchemy import select

from src.normalize import strip_accents


@dataclass(frozen=True)
class CanonicalCategory:
    key: str
    label_ru: str
    label_az: str


@dataclass(frozen=True)
class Rule:
    """One semantic signal for one canonical group.

    ``mode='prefix'`` matches the phrase at a word start and tolerates
    Azerbaijani agglutination (``agiz boslug`` matches ``ağız boşluğunun``).
    ``mode='word'`` requires a whole word and is used for short signals that
    would otherwise hide inside longer words (``bad``/БАД vs ``badam``/almond).

    ``segment_override`` marks the few signals that identify the mother/baby
    *customer segment* itself and may therefore outrank a body-system signal.
    It is a property of the SIGNAL, never of the canonical group: `hamile`
    (pregnancy) also lives in ``mother_baby`` but must not override, or
    "Hamiləlikdən qorunma vasitələri" (contraception — the semantic inverse of
    the segment) would be reported as "Мама и ребёнок".
    """

    id: str
    key: str
    phrase: str
    mode: Literal["prefix", "word"] = "prefix"
    supersedes: frozenset[str] = frozenset()
    segment_override: bool = False


@dataclass(frozen=True)
class Classification:
    """Detailed, auditable outcome of classifying one source category."""

    category: CanonicalCategory | None
    reason: str
    rule_ids: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()


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

# ─── Policy ──────────────────────────────────────────────────────────────────

# The taxonomy mixes a body-system axis with one customer-segment group
# (`mother_baby`). Both can legitimately fire for "kids' oral care". Rather than
# let rule order decide silently, the kids-segment SIGNALS win by an explicit,
# documented policy: every client (PharmOnline) kids bucket is merchandised by
# the client itself as `ushaq-*` (kids' skin care, kids' hair care), so grouping
# them under "Мама и ребёнок" mirrors the client's own tree. The audit lists
# every category resolved this way (`reason='segment_policy'`) so the policy
# stays reviewable.
#
# The override is carried by `Rule.segment_override` on those signals ONLY — not
# by the `mother_baby` key. Keying it on the group would silently promote
# `mb.hamile`/`mb.dogus`/`mb.laktasiya`, which are pregnancy/obstetric signals
# rather than segment signals: "Hamiləlikdən qorunma vasitələri (kontraseptivlər)"
# would then be filed under "Мама и ребёнок" despite being its inverse. Those
# signals now fail closed to `ambiguous` when they collide, which is where a
# contraceptive or a "vitamins for pregnant women" bucket belongs.
_SEGMENT_KEY = "mother_baby"

_RULES: tuple[Rule, ...] = (
    # ─── oncology ───
    Rule("onc.onkoloji", "oncology", "onkoloji"),
    Rule("onc.onkolog", "oncology", "onkolog"),
    Rule("onc.tumor", "oncology", "tumor"),
    Rule("onc.sis", "oncology", "sis xestelik"),
    # ─── homeopathy ───
    Rule("hom.homeopat", "homeopathy", "homeopat"),
    # ─── oral care ───
    Rule("oral.agiz", "oral_care", "agiz boslug"),
    Rule("oral.mecun", "oral_care", "dis mecun"),
    Rule("oral.firca", "oral_care", "dis firca"),
    Rule("oral.dental", "oral_care", "dental"),
    Rule("oral.stomatolog", "oral_care", "stomatolog"),
    # ─── eye health ───
    Rule("eye.goz_xest", "eye_health", "goz xest"),
    Rule("eye.gozun_xest", "eye_health", "gozun xest"),
    Rule("eye.quru_goz", "eye_health", "quru goz"),
    Rule("eye.oftalm", "eye_health", "oftalm"),
    Rule("eye.qlaukoma", "eye_health", "qlaukoma"),
    Rule("eye.katarakta", "eye_health", "katarakta"),
    Rule("eye.gorme", "eye_health", "gorme zeif"),
    # ─── mother & baby ───
    # Only the kids-SEGMENT signals carry `segment_override` (see `_SEGMENT_KEY`).
    Rule("mb.ana_usaq", "mother_baby", "ana ve usaq", segment_override=True),
    Rule("mb.usaq", "mother_baby", "usaq", segment_override=True),
    Rule("mb.korpe", "mother_baby", "korpe", segment_override=True),
    Rule("mb.baby", "mother_baby", "baby", mode="word", segment_override=True),
    Rule("mb.pediatr", "mother_baby", "pediatr", segment_override=True),
    # Pregnancy/obstetric signals: same group, but NOT segment markers. They must
    # fail closed on collision — `hamile` also fires on "protection FROM pregnancy".
    Rule("mb.hamile", "mother_baby", "hamile"),
    Rule("mb.laktasiya", "mother_baby", "laktasiya"),
    Rule("mb.dogus", "mother_baby", "dogus"),
    # ─── medical devices ───
    Rule("dev.tibbi_avadan", "medical_devices", "tibbi avadan"),
    Rule("dev.tibbi_vasite", "medical_devices", "tibbi vasite"),
    Rule("dev.tibbi_geyim", "medical_devices", "tibbi geyim"),
    Rule("dev.tonometr", "medical_devices", "tonometr"),
    Rule("dev.termometr", "medical_devices", "termometr"),
    Rule("dev.qlukometr", "medical_devices", "qlukometr"),
    Rule("dev.inhaliyator", "medical_devices", "inhaliyator"),
    Rule("dev.pulsoximetr", "medical_devices", "pulsoximetr"),
    Rule("dev.aparat", "medical_devices", "aparat"),
    Rule("dev.sargi", "medical_devices", "sargi material"),
    Rule("dev.kateter", "medical_devices", "kateter"),
    Rule("dev.zond", "medical_devices", "zond"),
    Rule("dev.spris", "medical_devices", "spris"),
    Rule("dev.siring", "medical_devices", "siring"),
    Rule("dev.tibbi_mask", "medical_devices", "tibbi mask"),
    # `respirator` alone means a respirator mask, but `respirator distress` is a
    # disease. The disease rule explicitly supersedes the device rule; without
    # this the generic signal silently swallowed the bucket.
    Rule("dev.respirator", "medical_devices", "respirator"),
    Rule("dev.elcek", "medical_devices", "tibbi elcek"),
    Rule("dev.optik", "medical_devices", "optik"),
    Rule("dev.linza", "medical_devices", "kontakt linza"),
    Rule("dev.cerrahi", "medical_devices", "cerrahi vasite"),
    Rule("dev.test", "medical_devices", "test ucun vasite"),
    Rule("dev.masaj", "medical_devices", "masaj"),
    # ─── dermatology ───
    Rule("derm.deri_xest", "dermatology", "deri xest"),
    Rule("derm.derinin_xest", "dermatology", "derinin xest"),
    Rule("derm.dermatit", "dermatology", "dermatit"),
    Rule("derm.ekzema", "dermatology", "ekzema"),
    Rule("derm.psoria", "dermatology", "psoria"),
    Rule("derm.sizanaq", "dermatology", "sizanaq"),
    Rule("derm.demrov", "dermatology", "demrov"),
    Rule("derm.deri_infeksi", "dermatology", "deri infeksi"),
    Rule("derm.dirnaq", "dermatology", "dirnaq xest"),
    Rule("derm.sac_xest", "dermatology", "sac xest"),
    Rule("derm.yaniq", "dermatology", "yaniq zamani"),
    # ─── personal care ───
    Rule("pc.deriye_qulluq", "personal_care", "deriye qulluq"),
    Rule("pc.derisine_qulluq", "personal_care", "derisine qulluq"),
    Rule("pc.uze_qulluq", "personal_care", "uze qulluq"),
    Rule("pc.bedene_qulluq", "personal_care", "bedene qulluq"),
    Rule("pc.saca_qulluq", "personal_care", "saca qulluq"),
    Rule("pc.sac_ucun", "personal_care", "sac ucun"),
    Rule("pc.sampun", "personal_care", "sampun"),
    Rule("pc.balzam", "personal_care", "balzam"),
    Rule("pc.kosmetik", "personal_care", "kosmetik"),
    Rule("pc.gigiyen", "personal_care", "gigiyen"),
    Rule("pc.intim", "personal_care", "intim gel"),
    Rule("pc.dus_gel", "personal_care", "dus gel"),
    Rule("pc.sabun", "personal_care", "sabun"),
    Rule("pc.epilyasiya", "personal_care", "epilyasiya"),
    Rule("pc.depilyasiya", "personal_care", "depilyasiya"),
    Rule("pc.ter", "personal_care", "ter eleyhine"),
    Rule("pc.dezodor", "personal_care", "dezodor"),
    Rule("pc.antiperspirant", "personal_care", "antiperspirant"),
    Rule("pc.etir", "personal_care", "etir", mode="word"),
    Rule("pc.parfum", "personal_care", "parfum"),
    Rule("pc.qulluq_vasite", "personal_care", "qulluq vasite"),
    Rule("pc.nemlendirici", "personal_care", "nemlendirici"),
    Rule("pc.temizleyici", "personal_care", "temizleyici"),
    Rule("pc.sac_boya", "personal_care", "sac boya"),
    # ─── vitamins & supplements ───
    Rule("vit.vitamin", "vitamins_supplements", "vitamin"),
    Rule("vit.mineral", "vitamins_supplements", "mineral"),
    Rule("vit.tebii", "vitamins_supplements", "tebii vasite"),
    Rule("vit.bitki_yagi", "vitamins_supplements", "bitki yag"),
    Rule("vit.bitki_cay", "vitamins_supplements", "bitki cay"),
    Rule("vit.derman_bitki", "vitamins_supplements", "derman bitki"),
    Rule("vit.baliq_yagi", "vitamins_supplements", "baliq yag"),
    Rule("vit.qida_elave", "vitamins_supplements", "qida elave"),
    Rule("vit.supplement", "vitamins_supplements", "supplement"),
    # `bad` = БАД (supplements). Whole-word only: as a prefix it would also
    # match `badam` (almond) and similar unrelated words.
    Rule("vit.bad", "vitamins_supplements", "bad", mode="word"),
    # ─── endocrine & metabolism ───
    Rule("endo.endokrin", "endocrine_metabolism", "endokrin"),
    Rule("endo.diabet", "endocrine_metabolism", "diabet"),
    Rule("endo.hipoqlikemik", "endocrine_metabolism", "hipoqlikemik"),
    Rule("endo.qalxanabenzer", "endocrine_metabolism", "qalxanabenzer"),
    Rule("endo.metabol", "endocrine_metabolism", "metabol"),
    Rule("endo.mubadile", "endocrine_metabolism", "maddeler mubadile"),
    Rule("endo.piylenme", "endocrine_metabolism", "piylenme"),
    Rule("endo.beden_cekisi", "endocrine_metabolism", "beden cekisi"),
    Rule("endo.qidalanma", "endocrine_metabolism", "qidalanma poz"),
    Rule("endo.dieta", "endocrine_metabolism", "dieta"),
    # ─── digestive ───
    Rule("dig.hezm", "digestive_system", "hezm"),
    Rule("dig.mede", "digestive_system", "mede"),
    Rule("dig.bagirsaq", "digestive_system", "bagirsaq"),
    Rule("dig.qaraciyer", "digestive_system", "qaraciyer"),
    Rule("dig.od_xest", "digestive_system", "od xest"),
    Rule("dig.qebizlik", "digestive_system", "qebizlik"),
    Rule("dig.isal", "digestive_system", "isal"),
    Rule("dig.qusma", "digestive_system", "qusma"),
    Rule("dig.urekbulanma", "digestive_system", "urekbulanma"),
    Rule("dig.kop", "digestive_system", "kop eleyhine"),
    Rule("dig.qastrit", "digestive_system", "qastrit"),
    Rule("dig.kolit", "digestive_system", "kolit"),
    Rule("dig.pankreas", "digestive_system", "pankreas"),
    Rule("dig.medealti", "digestive_system", "medealti"),
    # ─── urogenital & reproductive ───
    Rule("uro.sidik", "urogenital_reproductive", "sidik"),
    Rule("uro.boyrek", "urogenital_reproductive", "boyrek"),
    Rule("uro.prostat", "urogenital_reproductive", "prostat"),
    Rule("uro.qadin_sag", "urogenital_reproductive", "qadin sag"),
    Rule("uro.qadin_xest", "urogenital_reproductive", "qadin xest"),
    Rule("uro.ginekolo", "urogenital_reproductive", "ginekolo"),
    Rule("uro.menstru", "urogenital_reproductive", "menstru"),
    Rule("uro.klimaks", "urogenital_reproductive", "klimaks"),
    Rule("uro.menopauz", "urogenital_reproductive", "menopauz"),
    Rule("uro.sonsuzluq", "urogenital_reproductive", "sonsuzluq"),
    Rule("uro.potensiya", "urogenital_reproductive", "potensiya"),
    Rule("uro.kontrasept", "urogenital_reproductive", "kontrasept"),
    Rule("uro.cinsi", "urogenital_reproductive", "cinsi yolla"),
    Rule("uro.mastopatiya", "urogenital_reproductive", "mastopatiya"),
    # `uşaqlıq` = UTERUS, and folds to `usaqliq` — which starts with `usaq`
    # (child). Without this the kids signal fires on uterine-hypertonus and
    # vaginal-constrictor buckets by pure linguistic accident and, being a
    # segment_override, wins. Same shape as `respirator distress` vs `respirator`.
    # `usaqli` is deliberately not a prefix of `usaqlar`/`usaq qidasi`.
    Rule(
        "uro.usaqliq",
        "urogenital_reproductive",
        "usaqli",
        supersedes=frozenset({"mb.usaq"}),
    ),
    # ─── cardiovascular & blood ───
    Rule("cv.urek_damar", "cardiovascular_blood", "urek damar"),
    Rule("cv.qan_dovrani", "cardiovascular_blood", "qan dovrani"),
    Rule("cv.hipertoni", "cardiovascular_blood", "hipertoni"),
    Rule("cv.antihipertenziv", "cardiovascular_blood", "antihipertenziv"),
    Rule("cv.aritmi", "cardiovascular_blood", "aritmi"),
    Rule("cv.stenokard", "cardiovascular_blood", "stenokard"),
    Rule("cv.trombo", "cardiovascular_blood", "trombo"),
    Rule("cv.ateroskleroz", "cardiovascular_blood", "ateroskleroz"),
    Rule("cv.varikoz", "cardiovascular_blood", "varikoz"),
    Rule("cv.urek_catis", "cardiovascular_blood", "urek catis"),
    Rule("cv.qanaxma", "cardiovascular_blood", "qanaxma"),
    Rule("cv.anemiya", "cardiovascular_blood", "anemiya"),
    Rule("cv.qan_xest", "cardiovascular_blood", "qan xest"),
    Rule("cv.miokard", "cardiovascular_blood", "miokard"),
    Rule("cv.insult", "cardiovascular_blood", "insult"),
    # Cerebral circulation is vascular. `nervous_system` deliberately does NOT
    # carry a `beyin qan` signal: the same phrase in two groups would be a
    # silent, order-dependent coin flip (and `_validate_rules` now rejects it).
    Rule("cv.beyin_qan", "cardiovascular_blood", "beyin qan"),
    # ─── nervous system ───
    Rule("ns.sinir", "nervous_system", "sinir sistemi"),
    Rule("ns.yuxu", "nervous_system", "yuxu poz"),
    Rule("ns.depress", "nervous_system", "depress"),
    Rule("ns.epilep", "nervous_system", "epilep"),
    Rule("ns.sizofren", "nervous_system", "sizofren"),
    Rule("ns.parkinson", "nervous_system", "parkinson"),
    Rule("ns.migren", "nervous_system", "migren"),
    Rule("ns.neyropati", "nervous_system", "neyropati"),
    Rule("ns.nevral", "nervous_system", "nevral"),
    Rule("ns.beyin_fealiyy", "nervous_system", "beyin fealiyy"),
    Rule("ns.sakitles", "nervous_system", "sakitles"),
    Rule("ns.asten", "nervous_system", "asten"),
    Rule("ns.yorgunluq", "nervous_system", "yorgunluq"),
    Rule("ns.sinir_heyecan", "nervous_system", "sinir heyecan"),
    # ─── pain & musculoskeletal ───
    Rule("pain.agrikesici", "pain_musculoskeletal", "agrikesici"),
    Rule("pain.agri_zamani", "pain_musculoskeletal", "agri zamani"),
    Rule("pain.agri_eleyhine", "pain_musculoskeletal", "agri eleyhine"),
    Rule("pain.sumuk", "pain_musculoskeletal", "sumuk ezele"),
    Rule("pain.oynaq", "pain_musculoskeletal", "oynaq"),
    Rule("pain.artrit", "pain_musculoskeletal", "artrit"),
    Rule("pain.artroz", "pain_musculoskeletal", "artroz"),
    Rule("pain.xondro", "pain_musculoskeletal", "xondro"),
    Rule("pain.osteoporoz", "pain_musculoskeletal", "osteoporoz"),
    Rule("pain.raxit", "pain_musculoskeletal", "raxit"),
    Rule("pain.podaqra", "pain_musculoskeletal", "podaqra"),
    Rule("pain.ortoped", "pain_musculoskeletal", "ortoped"),
    Rule("pain.miorelaksant", "pain_musculoskeletal", "miorelaksant"),
    Rule("pain.spazmolitik", "pain_musculoskeletal", "spazmolitik"),
    Rule("pain.dayaq", "pain_musculoskeletal", "dayaq hereket"),
    # ─── respiratory & ENT ───
    Rule("resp.teneffus", "respiratory_ent", "teneffus"),
    Rule("resp.bbq", "respiratory_ent", "burun bogaz qulaq"),
    Rule("resp.burun", "respiratory_ent", "burun"),
    Rule("resp.bogaz", "respiratory_ent", "bogaz"),
    Rule("resp.qulaq_xest", "respiratory_ent", "qulaq xest"),
    Rule("resp.oskurek", "respiratory_ent", "oskurek"),
    Rule("resp.qrip", "respiratory_ent", "qrip"),
    Rule("resp.bronx", "respiratory_ent", "bronx"),
    Rule("resp.pnevmon", "respiratory_ent", "pnevmon"),
    Rule("resp.astma", "respiratory_ent", "astma"),
    Rule("resp.rinit", "respiratory_ent", "rinit"),
    Rule("resp.sinusit", "respiratory_ent", "sinusit"),
    Rule("resp.faringit", "respiratory_ent", "faringit"),
    Rule("resp.laringit", "respiratory_ent", "laringit"),
    Rule("resp.angina", "respiratory_ent", "angina"),
    Rule(
        "resp.respirator_distress",
        "respiratory_ent",
        "respirator distress",
        supersedes=frozenset({"dev.respirator"}),
    ),
    # ─── infections & immunity ───
    Rule("inf.bakterial", "infectious_immune", "bakterial"),
    Rule("inf.antibakterial", "infectious_immune", "antibakterial"),
    Rule("inf.virus", "infectious_immune", "virus"),
    Rule("inf.gobelek", "infectious_immune", "gobelek infeksi"),
    Rule("inf.antiparazitar", "infectious_immune", "antiparazitar"),
    Rule("inf.parazitar", "infectious_immune", "parazitar"),
    Rule("inf.antihelmint", "infectious_immune", "antihelmint"),
    Rule("inf.qurd", "infectious_immune", "qurd xest"),
    Rule("inf.infeksion", "infectious_immune", "infeksion"),
    Rule("inf.immunitet", "infectious_immune", "immunitet"),
    # Was `immuncatish` — a spelling that can never occur. Real text folds to
    # `immun catismazligi` (İmmun çatışmazlığı).
    Rule("inf.immun_catis", "infectious_immune", "immun catis"),
    Rule("inf.immun_defisit", "infectious_immune", "immun defisit"),
    Rule("inf.allergik", "infectious_immune", "allergik reaksi"),
    Rule("inf.anafilakt", "infectious_immune", "anafilakt"),
)

# Dosage form / administration route / potency-class buckets. These describe HOW
# a product is delivered, never WHAT it treats, so they can never be a defensible
# reporting category. Matched on the whole normalized slug (with any trailing
# `-N` shard index removed), never as a substring: `aghiz-boshlughu-uchun-mehlullar`
# is legitimately oral care and must keep classifying.
_FORM_SLUGS: frozenset[str] = frozenset(
    {
        "inyeksiyalar",
        "inyeksiyalar ve infuziyalar",
        "infuziyalar",
        "samlar",
        "xarici vasiteler",
        "xarici vasiteler ve inyeksiyalar",
        "yerli vasiteler",
        "peroral vasiteler tabletler kapsullar",
        "tabletler",
        "kapsullar",
        "mehlullar",
        "guclu tesiredici vasiteler",
    }
)

# Broad department buckets that carry an entire catalogue (Aloe's `dermanlar`
# holds 5.4k products across every therapeutic area). Grouping them would be a
# label, not a fact.
_BROAD_SLUGS: dict[str, frozenset[str]] = {
    "aloe": frozenset({"dermanlar", "tibbi vasiteler"}),
}

_TOKEN_CHARS = "a-z0-9а-яё"
_SPACE_RE = re.compile(rf"[^{_TOKEN_CHARS}]+", re.IGNORECASE)
_SHARD_SUFFIX_RE = re.compile(r"\s+\d+$")
# PharmOnline slugs transliterate Azerbaijani with digraphs while labels use
# diacritics that `strip_accents` folds to a single letter. Collapse both to one
# canonical spelling so the same concept classifies identically on every site.
_DIGRAPH_FOLDS: tuple[tuple[str, str], ...] = (("sh", "s"), ("ch", "c"), ("gh", "g"))


def _fold(text: str) -> str:
    """Collapse both AZ spellings of the same sound to one form.

    NB: not idempotent, and convergence is not universal. A digraph followed by
    a real `h` diverges — `məşhur` folds to `mesur` while the transliterated
    `meshhur` folds to `meshur`. No current signal has that shape and such words
    are rare in this catalogue, but a phrase like `mesur` would pass the
    folded-form validator while never matching the digraph spelling. Check any
    new signal containing `sh`/`ch`/`gh` + `h` by hand.
    """
    out = strip_accents(text).casefold()
    for digraph, letter in _DIGRAPH_FOLDS:
        out = out.replace(digraph, letter)
    return out


def _normalized_text(*values: str | None) -> str:
    combined = " ".join(value for value in values if value)
    return " ".join(_SPACE_RE.sub(" ", _fold(combined)).split())


def _validate_rules(rules: tuple[Rule, ...]) -> list[str]:
    """Static defects that would make classification order-dependent.

    Returns human-readable problems instead of raising so the audit can report
    them under ``--strict`` while the API keeps serving.
    """
    problems: list[str] = []
    seen_ids: set[str] = set()
    by_phrase: dict[tuple[str, str], set[str]] = {}
    for rule in rules:
        if rule.id in seen_ids:
            problems.append(f"duplicate rule id: {rule.id}")
        seen_ids.add(rule.id)
        if rule.phrase != _normalized_text(rule.phrase):
            problems.append(
                f"{rule.id}: phrase {rule.phrase!r} is not in canonical folded form "
                f"(expected {_normalized_text(rule.phrase)!r})"
            )
        if rule.key not in _BY_KEY:
            problems.append(f"{rule.id}: unknown canonical key {rule.key!r}")
        by_phrase.setdefault((rule.phrase, rule.mode), set()).add(rule.key)
    for (phrase, mode), keys in sorted(by_phrase.items()):
        if len(keys) > 1:
            problems.append(
                f"phrase {phrase!r} (mode={mode}) maps to multiple canonical keys "
                f"{sorted(keys)} — order would silently decide"
            )
    for rule in rules:
        for target in sorted(rule.supersedes):
            if target not in seen_ids:
                problems.append(f"{rule.id}: supersedes unknown rule id {target!r}")
        if rule.id in rule.supersedes:
            problems.append(f"{rule.id}: supersedes itself")
        if rule.segment_override and rule.key != _SEGMENT_KEY:
            problems.append(
                f"{rule.id}: segment_override set on key {rule.key!r}, "
                f"only {_SEGMENT_KEY!r} may carry it"
            )
    # A supersede cycle passes every check above and then annihilates: both rules
    # veto each other, nothing stays live, and the category silently vanishes.
    supersedes_by_id = {rule.id: rule.supersedes for rule in rules}
    for cycle_id in sorted(supersedes_by_id):
        seen: set[str] = set()
        stack = [cycle_id]
        while stack:
            current = stack.pop()
            for target in supersedes_by_id.get(current, frozenset()):
                if target == cycle_id:
                    problems.append(
                        f"supersedes cycle involving {cycle_id!r} — both signals would "
                        f"veto each other and the category would vanish silently"
                    )
                    stack = []
                    break
                if target not in seen:
                    seen.add(target)
                    stack.append(target)
    return problems


def rule_validation_problems() -> list[str]:
    """Public hook for the audit / CI to assert the rule set is well-formed."""
    return _validate_rules(_RULES)


@lru_cache(maxsize=None)
def _pattern_for(phrase: str, mode: str) -> re.Pattern[str]:
    """Compile once per rule: `_matches` runs ~200×3 times per category."""
    start = rf"(?<![{_TOKEN_CHARS}])"
    end = rf"(?![{_TOKEN_CHARS}])" if mode == "word" else ""
    return re.compile(start + re.escape(phrase) + end)


def _matches(rule: Rule, text: str) -> bool:
    if not text:
        return False
    return _pattern_for(rule.phrase, rule.mode).search(text) is not None


def canonical_category(key: str) -> CanonicalCategory | None:
    return _BY_KEY.get(key)


def classify_source_category_detailed(
    site: str,
    slug: str | None,
    *,
    label_ru: str | None = None,
    label_az: str | None = None,
) -> Classification:
    """Map one source category to the canonical taxonomy, with audit detail.

    Collects every matching rule across slug and labels, then resolves. Rule
    order is irrelevant by construction. Anything unresolved stays unmapped.
    """
    if not slug:
        return Classification(None, "no_slug")

    slug_text = _normalized_text(slug)
    shard_free = _SHARD_SUFFIX_RE.sub("", slug_text)
    if shard_free in _BROAD_SLUGS.get(site, frozenset()):
        return Classification(None, "blocked_broad_bucket")
    if shard_free in _FORM_SLUGS:
        return Classification(None, "blocked_dosage_form")

    # Match each field separately: a phrase must never span the seam between a
    # slug and an unrelated label.
    fields = [slug_text, _normalized_text(label_ru), _normalized_text(label_az)]
    matched = [rule for rule in _RULES if any(_matches(rule, field) for field in fields)]
    if not matched:
        return Classification(None, "no_signal")

    # A supersede veto says "when my phrase matches, that other signal is a false
    # positive IN THIS TEXT" — it is a statement about the text, not about the
    # vetoing rule's own liveness. So vetoes are collected from every matched
    # rule BEFORE filtering, and a superseded rule still vetoes. Do not "fix"
    # this into a poset walk; it is also what makes specificity chains resolve.
    superseded: set[str] = set()
    for rule in matched:
        superseded |= rule.supersedes
    live = [rule for rule in matched if rule.id not in superseded]
    if not live:
        # Only reachable via a supersede cycle, which `_validate_rules` rejects.
        # Distinct reason so the audit never mistakes it for "no rule matched".
        return Classification(None, "superseded_out", tuple(sorted(rule.id for rule in matched)))

    keys = {rule.key for rule in live}
    rule_ids = tuple(sorted(rule.id for rule in live))

    if len(keys) == 1:
        return Classification(_BY_KEY[next(iter(keys))], "matched", rule_ids, tuple(sorted(keys)))

    # Documented segment policy (see `_SEGMENT_KEY`): a KIDS bucket is reported
    # under "Мама и ребёнок" even when a body-system signal also fires. Driven by
    # the signal, not the group — pregnancy signals deliberately do not qualify.
    if any(rule.segment_override for rule in live):
        return Classification(
            _BY_KEY[_SEGMENT_KEY], "segment_policy", rule_ids, tuple(sorted(keys))
        )

    # Two unrelated groups both fire and nothing resolves it → fail closed.
    return Classification(None, "ambiguous", rule_ids, tuple(sorted(keys)))


def classify_source_category(
    site: str,
    slug: str | None,
    *,
    label_ru: str | None = None,
    label_az: str | None = None,
) -> CanonicalCategory | None:
    """Map one clear source category to the compact canonical taxonomy.

    Thin wrapper over :func:`classify_source_category_detailed` for callers that
    only need the outcome. Unknown, ambiguous or format-only source categories
    return ``None`` and therefore never create dashboard noise.
    """
    return classify_source_category_detailed(
        site, slug, label_ru=label_ru, label_az=label_az
    ).category


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
