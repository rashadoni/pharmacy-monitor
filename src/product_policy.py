"""Trusted product identity and current-offer policy.

The matching model intentionally separates two concepts:

* manufacturer country is part of physical SKU identity;
* website availability is a time-varying offer state.

Country values are normalized only from explicit site signals.  Availability is
tri-state: a missing signal is ``unknown``, never an implicit out-of-stock.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, desc, func, or_, select

from src._time import utcnow
from src.normalize import strip_accents

COUNTRY_RESOLVED = "resolved"
COUNTRY_UNKNOWN = "unknown"
COUNTRY_AMBIGUOUS = "ambiguous"
COUNTRY_INVALID = "invalid"

OFFER_IN_STOCK = "in_stock"
OFFER_OUT_OF_STOCK = "out_of_stock"
OFFER_UNKNOWN = "unknown"

COUNTRY_POLICY_MODES = {"shadow", "enforce"}
AVAILABILITY_POLICY_MODES = {"shadow", "enforce"}

# Daily full-catalog cadence plus a safety margin.  A stale observation is not
# a trusted active offer, but it is also not rewritten to out_of_stock.  The
# systemd schedule is installed by infra/scripts/install_systemd_schedule.sh;
# keep this business promise in code instead of silently accepting a weekly
# timer override.
OFFER_MAX_AGE_HOURS = {
    "aloe": 30,
    "aptekonline": 30,
    "pharmonline": 30,
}

REQUIRED_CATALOG_SITES = ("pharmonline", "aptekonline", "aloe")
_MIN_POLICY_COVERAGE_PCT = 98.0

# A full-catalog producer must calculate its financial outputs before the Run
# becomes externally visible.  The context is deliberately process-local and
# scoped to one Run: API requests never inherit it, so they continue to fail
# closed while the latest full attempt is still ``running``.
_FINALIZING_TRUSTED_RUN_ID: ContextVar[int | None] = ContextVar(
    "pharmacy_monitor_finalizing_trusted_run_id", default=None
)


@contextmanager
def finalizing_trusted_run(run_id: int):
    """Allow internal consumers to use one verified, still-running full Run."""
    token = _FINALIZING_TRUSTED_RUN_ID.set(run_id)
    try:
        yield
    finally:
        _FINALIZING_TRUSTED_RUN_ID.reset(token)


def is_finalizing_trusted_run(run_id: int | None) -> bool:
    """Return true only inside the scoped internal finalization window."""
    return run_id is not None and _FINALIZING_TRUSTED_RUN_ID.get() == run_id


# ISO-3166 alpha-2 allowlist.  Accepting arbitrary two-letter strings (``xx``)
# would turn dirty source values into false identity conflicts and auto-splits.
_ISO_ALPHA2 = frozenset(
    """
    ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj
    bl bm bn bo bq br bs bt bv bw by bz ca cc cd cf cg ch ci ck cl cm cn co cr
    cu cv cw cx cy cz de dj dk dm do dz ec ee eg eh er es et fi fj fk fm fo fr
    ga gb gd ge gf gg gh gi gl gm gn gp gq gr gs gt gu gw gy hk hm hn hr ht hu
    id ie il im in io iq ir is it je jm jo jp ke kg kh ki km kn kp kr kw ky kz
    la lb lc li lk lr ls lt lu lv ly ma mc md me mf mg mh mk ml mm mn mo mp mq
    mr ms mt mu mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf
    pg ph pk pl pm pn pr ps pt pw py qa re ro rs ru rw sa sb sc sd se sg sh si
    sj sk sl sm sn so sr ss st sv sx sy sz tc td tf tg th tj tk tl tm tn to tr
    tt tv tw tz ua ug um us uy uz va vc ve vg vi vn vu wf ws ye yt za zm zw
    """.split()
)


def _country_key(raw: str) -> str:
    return " ".join(strip_accents(raw).lower().replace("-", " ").split())


_COUNTRY_ALIASES: dict[str, str] = {
    "turkiye": "tr",
    "turkiya": "tr",
    "turkey": "tr",
    "турция": "tr",
    "rusiya": "ru",
    "russia": "ru",
    "rossiya": "ru",
    "россия": "ru",
    "azerbaycan": "az",
    "azerbaijan": "az",
    "азербайджан": "az",
    "fransa": "fr",
    "france": "fr",
    "франция": "fr",
    "almaniya": "de",
    "germany": "de",
    "ger": "de",
    "germaniya": "de",
    "германия": "de",
    "ukrayna": "ua",
    "ukraina": "ua",
    "ukraine": "ua",
    "украина": "ua",
    "polsa": "pl",
    "polsha": "pl",
    "poland": "pl",
    "polonya": "pl",
    "польша": "pl",
    "italiya": "it",
    "italy": "it",
    "италия": "it",
    "cin": "cn",
    "chin": "cn",
    "china": "cn",
    "китай": "cn",
    "hindistan": "in",
    "india": "in",
    "индия": "in",
    "belarus": "by",
    "беларусь": "by",
    "macaristan": "hu",
    "hungary": "hu",
    "венгрия": "hu",
    "ispaniya": "es",
    "spain": "es",
    "испания": "es",
    "bolqaristan": "bg",
    "bulgaria": "bg",
    "болгария": "bg",
    "latviya": "lv",
    "latvia": "lv",
    "латвия": "lv",
    "sloveniya": "si",
    "slovenia": "si",
    "словения": "si",
    "abs": "us",
    "usa": "us",
    "amerika": "us",
    "united states": "us",
    "сша": "us",
    "yaponiya": "jp",
    "japan": "jp",
    "япония": "jp",
    "cexiya": "cz",
    "chexiya": "cz",
    "czech": "cz",
    "чехия": "cz",
    "niderland": "nl",
    "hollandiya": "nl",
    "netherlands": "nl",
    "нидерланды": "nl",
    "isvecre": "ch",
    "isveckre": "ch",
    "switzerland": "ch",
    "швейцария": "ch",
    "isvec": "se",
    "sweden": "se",
    "швеция": "se",
    "avstriya": "at",
    "austria": "at",
    "австрия": "at",
    "koreya": "kr",
    "korea": "kr",
    "корея": "kr",
    "ingiltere": "gb",
    "england": "gb",
    "great britain": "gb",
    "united kingdom": "gb",
    "uk": "gb",
    "angliya": "gb",
    "англия": "gb",
    "великобритания": "gb",
    "misir": "eg",
    "egypt": "eg",
    "египет": "eg",
    "iran": "ir",
    "иран": "ir",
    "pakistan": "pk",
    "пакистан": "pk",
    "vyetnam": "vn",
    "vietnam": "vn",
    "вьетнам": "vn",
    "yunanistan": "gr",
    "greece": "gr",
    "греция": "gr",
    "rumıniya": "ro",
    "rumeniya": "ro",
    "romania": "ro",
    "румыния": "ro",
    "portuqaliya": "pt",
    "portugal": "pt",
    "португалия": "pt",
    "belcika": "be",
    "belgium": "be",
    "бельгия": "be",
    "danimarka": "dk",
    "denmark": "dk",
    "дания": "dk",
    "finlandiya": "fi",
    "finland": "fi",
    "финляндия": "fi",
    "norvec": "no",
    "norway": "no",
    "норвегия": "no",
    "litva": "lt",
    "литва": "lt",
    "estoniya": "ee",
    "эстония": "ee",
    "xorvatiya": "hr",
    "croatia": "hr",
    "хорватия": "hr",
    "serbiya": "rs",
    "serbia": "rs",
    "сербия": "rs",
    "gurcustan": "ge",
    "georgia": "ge",
    "грузия": "ge",
    "kazakhstan": "kz",
    "казахстан": "kz",
    "ozbekistan": "uz",
    "uzbekistan": "uz",
    "узбекистан": "uz",
    "birlesmis kralliq": "gb",
    "boyuk britaniya": "gb",
    "britaniya": "gb",
    "moldova": "md",
    "bosniya ve herseqovina": "ba",
    "bosniya": "ba",
    "banqlades": "bd",
    "banglades": "bd",
    "bangladesh": "bd",
    "malayziya": "my",
    "malaziya": "my",
    "irelandiya": "ie",
    "slovakiya": "sk",
    "tailand": "th",
    "tayland": "th",
    "kipr": "cy",
    "sinqapur": "sg",
    "bee": "ae",
    "birlesmis ereb emirlikleri": "ae",
    "avstraliya": "au",
    "israil": "il",
    "kanada": "ca",
    "braziliya": "br",
    "makedoniya": "mk",
    "simali makedoniya respublikasi": "mk",
    "malta": "mt",
    "indoneziya": "id",
    "turkmenistan": "tm",
    "qazaxistan": "kz",
    "iordaniya": "jo",
    "seudiye erebistani": "sa",
    "yeni zellandiya": "nz",
    "honq konq": "hk",
    "hong kong": "hk",
    "cenubi afrika": "za",
    "cenubi koreya": "kr",
    "koreya respublikasi": "kr",
    "islandiya": "is",
    "tayvan": "tw",
    "merakes": "ma",
    "monteneqro": "me",
    "oman": "om",
    "monako": "mc",
    "paraqvay": "py",
    "uruqvay": "uy",
    "kolumbiya": "co",
    "meksika": "mx",
    "peru": "pe",
}

# Source catalogs also contain ISO alpha-3 and a handful of established local
# three-letter exports.  The map is explicit: unknown codes remain invalid.
_ISO_ALPHA3_TO_ALPHA2: dict[str, str] = {
    "alb": "al",
    "arg": "ar",
    "aze": "az",
    "aus": "au",
    "aut": "at",
    "bel": "be",
    "bgd": "bd",
    "bgr": "bg",
    "bol": "bo",
    "bra": "br",
    "can": "ca",
    "che": "ch",
    "chn": "cn",
    "cin": "cn",
    "col": "co",
    "cyp": "cy",
    "cze": "cz",
    "deu": "de",
    "ger": "de",
    "dnk": "dk",
    "egy": "eg",
    "eng": "gb",
    "esp": "es",
    "est": "ee",
    "fin": "fi",
    "fra": "fr",
    "gbr": "gb",
    "geo": "ge",
    "grc": "gr",
    "gre": "gr",
    "hrv": "hr",
    "hun": "hu",
    "idn": "id",
    "ind": "in",
    "irl": "ie",
    "irn": "ir",
    "isr": "il",
    "ita": "it",
    "jap": "jp",
    "jpn": "jp",
    "jor": "jo",
    "kaz": "kz",
    "kor": "kr",
    "lva": "lv",
    "lat": "lv",
    "ltu": "lt",
    "mda": "md",
    "mkd": "mk",
    "mys": "my",
    "mal": "my",
    "nld": "nl",
    "nor": "no",
    "nzl": "nz",
    "pak": "pk",
    "pol": "pl",
    "prt": "pt",
    "rou": "ro",
    "rum": "ro",
    "rus": "ru",
    "sau": "sa",
    "sgp": "sg",
    "srb": "rs",
    "svk": "sk",
    "svn": "si",
    "swe": "se",
    "tha": "th",
    "tay": "th",
    "tkm": "tm",
    "tur": "tr",
    "ukr": "ua",
    "usa": "us",
    "uzb": "uz",
    "vnm": "vn",
    "zaf": "za",
    # Stable non-ISO exports observed in Aptekonline.
    "isp": "es",
    "ior": "jo",
    "xor": "hr",
    "avs": "at",
}


def normalize_country_code(raw: str | None) -> str | None:
    """Return ISO-3166 alpha-2 for a known textual country value."""
    if raw is None:
        return None
    value = str(raw).strip()
    if not value or value.lower() in {"none", "null", "unknown"}:
        return None
    if len(value) == 2 and value.isalpha():
        code = value.lower()
        return code if code in _ISO_ALPHA2 else None
    if len(value) == 3 and value.isalpha():
        code = _ISO_ALPHA3_TO_ALPHA2.get(value.lower())
        if code is not None:
            return code
    return _COUNTRY_ALIASES.get(_country_key(value))


def country_resolution(raw: str | None) -> tuple[str | None, str]:
    """Resolve a raw country signal without guessing numeric/unknown values."""
    if raw is None or not str(raw).strip():
        return None, COUNTRY_UNKNOWN
    code = normalize_country_code(str(raw))
    if code:
        return code, COUNTRY_RESOLVED
    if str(raw).strip().isdigit():
        return None, COUNTRY_AMBIGUOUS
    return None, COUNTRY_INVALID


def offer_from_quantity(raw: Any) -> tuple[str, float | None]:
    """Resolve an explicit stock quantity without treating missing data as OOS."""
    if raw is None or raw == "":
        return OFFER_UNKNOWN, None
    try:
        quantity = float(raw)
    except (TypeError, ValueError):
        return OFFER_UNKNOWN, None
    if quantity < 0:
        return OFFER_UNKNOWN, None
    return (OFFER_IN_STOCK if quantity > 0 else OFFER_OUT_OF_STOCK), quantity


def offer_from_boolean(raw: Any) -> tuple[str, float | None]:
    """Resolve only explicit booleans; strings are deliberately not guessed."""
    if raw is True:
        return OFFER_IN_STOCK, None
    if raw is False:
        return OFFER_OUT_OF_STOCK, None
    return OFFER_UNKNOWN, None


def policy_mode(name: str, default: str = "shadow") -> str:
    value = os.getenv(name, default).strip().lower()
    return value if value in COUNTRY_POLICY_MODES else default


def country_policy_enforced() -> bool:
    return policy_mode("COUNTRY_IDENTITY_POLICY") == "enforce"


def availability_policy_enforced() -> bool:
    return policy_mode("OFFER_AVAILABILITY_POLICY") == "enforce"


def policy_fingerprint() -> str:
    """Stable cache discriminator for rollout-sensitive financial output."""
    return (
        f"country={policy_mode('COUNTRY_IDENTITY_POLICY')};"
        f"availability={policy_mode('OFFER_AVAILABILITY_POLICY')}"
    )


def full_catalog_trust_report(
    session: Any,
    *,
    tenant_id: int = 1,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the fail-closed rollout evidence for every required site.

    Coverage is measured only over the site's current cadence window.  A run
    counts as evidence only when the producer explicitly marked an unbounded
    category scan as verified; a successful watchlist or limited scan can
    never open the financial gate.
    """
    from src import storage

    current = now or utcnow()
    sites: list[dict[str, Any]] = []
    for site in REQUIRED_CATALOG_SITES:
        max_age_hours = OFFER_MAX_AGE_HOURS[site]
        cutoff = current - timedelta(hours=max_age_hours)
        # The latest *attempt* is authoritative. Falling back to an older
        # verified run after a newer full scan lost pages would keep financial
        # output open precisely while the source is known to be degraded.
        latest_attempt = session.scalar(
            select(storage.Run)
            .where(
                storage.Run.tenant_id == tenant_id,
                storage.Run.catalog_scope == "full",
                ("," + storage.Run.full_catalog_sites + ",").like(f"%,{site},%"),
            )
            .order_by(desc(storage.Run.id))
            .limit(1)
        )
        finalizing_run_id = _FINALIZING_TRUSTED_RUN_ID.get()
        published = bool(
            latest_attempt is not None
            and latest_attempt.status == "ok"
            and bool(latest_attempt.catalog_verified)
        )
        internal_finalization = bool(
            latest_attempt is not None
            and latest_attempt.id == finalizing_run_id
            and latest_attempt.status == "running"
            and bool(latest_attempt.catalog_verified)
        )
        relevant_run = latest_attempt if published or internal_finalization else None
        run_at = (
            (relevant_run.finished_at or relevant_run.started_at)
            if relevant_run is not None
            else None
        )
        run_fresh = bool(run_at is not None and run_at >= cutoff)

        # Trust evidence is immutable and run-scoped.  Mutable Product state
        # can be refreshed by a later partial/watchlist run and must never make
        # an older full run appear more complete than it actually was.
        observation_filters = (
            storage.OfferObservation.tenant_id == tenant_id,
            storage.OfferObservation.run_id
            == (relevant_run.id if relevant_run is not None else -1),
            storage.Product.site == site,
        )
        total = (
            session.scalar(
                select(func.count(func.distinct(storage.OfferObservation.product_id)))
                .join(
                    storage.Product,
                    storage.Product.id == storage.OfferObservation.product_id,
                )
                .where(*observation_filters)
            )
            or 0
        )
        country_resolved = (
            session.scalar(
                select(func.count(func.distinct(storage.OfferObservation.product_id)))
                .join(
                    storage.Product,
                    storage.Product.id == storage.OfferObservation.product_id,
                )
                .where(
                    *observation_filters,
                    storage.OfferObservation.country_resolution_status == COUNTRY_RESOLVED,
                    storage.OfferObservation.country_code.is_not(None),
                )
            )
            or 0
        )
        availability_known = (
            session.scalar(
                select(func.count(func.distinct(storage.OfferObservation.product_id)))
                .join(
                    storage.Product,
                    storage.Product.id == storage.OfferObservation.product_id,
                )
                .where(
                    *observation_filters,
                    storage.OfferObservation.availability_status.in_(
                        (OFFER_IN_STOCK, OFFER_OUT_OF_STOCK)
                    ),
                )
            )
            or 0
        )
        country_pct = round(country_resolved / total * 100, 2) if total else 0.0
        availability_pct = round(availability_known / total * 100, 2) if total else 0.0
        country_ready = total > 0 and country_pct >= _MIN_POLICY_COVERAGE_PCT
        availability_ready = total > 0 and availability_pct >= _MIN_POLICY_COVERAGE_PCT
        sites.append(
            {
                "site": site,
                "products": total,
                "country_resolved": country_resolved,
                "country_coverage_pct": country_pct,
                "availability_known": availability_known,
                "availability_coverage_pct": availability_pct,
                "latest_full_attempt_id": (latest_attempt.id if latest_attempt else None),
                "latest_full_attempt_status": (latest_attempt.status if latest_attempt else None),
                "latest_full_attempt_verified": bool(
                    latest_attempt and latest_attempt.catalog_verified
                ),
                "latest_full_attempt_reason": (
                    latest_attempt.catalog_verification_reason if latest_attempt else None
                ),
                "full_catalog_run_id": relevant_run.id if relevant_run else None,
                "full_catalog_at": run_at,
                "full_catalog_fresh": run_fresh,
                "internal_finalization": internal_finalization,
                "country_ready": country_ready,
                "availability_ready": availability_ready,
                "ready": run_fresh and country_ready and availability_ready,
            }
        )

    country_mode = "enforce" if country_policy_enforced() else "shadow"
    availability_mode = "enforce" if availability_policy_enforced() else "shadow"
    country_ready = all(row["full_catalog_fresh"] and row["country_ready"] for row in sites)
    availability_ready = all(
        row["full_catalog_fresh"] and row["availability_ready"] for row in sites
    )
    policy_ready = (not country_policy_enforced() or country_ready) and (
        not availability_policy_enforced() or availability_ready
    )
    return {
        "country_mode": country_mode,
        "availability_mode": availability_mode,
        "full_catalog_trust_ready": all(row["ready"] for row in sites),
        "country_trust_ready": country_ready,
        "availability_trust_ready": availability_ready,
        "policy_ready": policy_ready,
        "sites": sites,
    }


def trusted_catalog_epoch(
    session: Any,
    *,
    tenant_id: int = 1,
    now: datetime | None = None,
) -> str | None:
    """Fingerprint the exact full-catalog observations behind financial data.

    Policy coverage and policy mode are separate concerns.  The epoch exists
    whenever every required site has a fresh verified full Run; enforce-mode
    callers additionally pass through ``policy_rollout_eligibility``.
    """
    report = full_catalog_trust_report(session, tenant_id=tenant_id, now=now)
    parts: list[str] = []
    for row in report["sites"]:
        run_id = row["full_catalog_run_id"]
        if run_id is None or not row["full_catalog_fresh"]:
            return None
        parts.append(f"{row['site']}:{run_id}")
    return "v1|" + "|".join(parts)


def policy_rollout_eligibility(
    session: Any,
    *,
    tenant_id: int = 1,
    now: datetime | None = None,
) -> Eligibility:
    """Fail closed when an enforced policy lacks full-catalog evidence."""
    if not country_policy_enforced() and not availability_policy_enforced():
        return Eligibility(True)
    report = full_catalog_trust_report(session, tenant_id=tenant_id, now=now)
    if not report["policy_ready"]:
        return Eligibility(False, "full_catalog_trust_not_ready")
    return Eligibility(True)


def country_code_of(product: Any) -> str | None:
    if getattr(product, "country_resolution_status", None) == COUNTRY_RESOLVED:
        return getattr(product, "manufacturer_country_code", None)
    return None


def country_conflicts(a: Any, b: Any) -> bool:
    ca, cb = country_code_of(a), country_code_of(b)
    return bool(ca and cb and ca != cb)


def offer_is_fresh(product: Any, *, now: datetime | None = None) -> bool:
    observed_at = getattr(product, "availability_observed_at", None)
    if observed_at is None:
        return False
    current = now or utcnow()
    max_age = OFFER_MAX_AGE_HOURS.get(getattr(product, "site", ""), 30)
    return observed_at >= current - timedelta(hours=max_age)


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reason: str | None = None


def current_offer_eligibility(product: Any, *, now: datetime | None = None) -> Eligibility:
    if getattr(product, "url_dead_at", None) is not None:
        return Eligibility(False, "dead_url")
    status = getattr(product, "offer_availability_status", OFFER_UNKNOWN) or OFFER_UNKNOWN
    if status == OFFER_OUT_OF_STOCK:
        return Eligibility(False, "out_of_stock")
    if status != OFFER_IN_STOCK:
        return Eligibility(False, "availability_unknown")
    if not offer_is_fresh(product, now=now):
        return Eligibility(False, "availability_stale")
    return Eligibility(True)


def identity_eligibility(products: list[Any]) -> Eligibility:
    codes = {country_code_of(p) for p in products}
    # A third unknown offer must never mask a proven conflict between two
    # resolved countries.  Check the known identity set first, then report
    # incomplete coverage only when the resolved values do not conflict.
    known_codes = codes - {None}
    if len(known_codes) > 1:
        return Eligibility(False, "country_conflict")
    if None in codes:
        return Eligibility(False, "country_unknown")
    return Eligibility(True)


def financially_eligible(products: list[Any], *, now: datetime | None = None) -> Eligibility:
    identity = identity_eligibility(products)
    if not identity.eligible:
        return identity
    for product in products:
        offer = current_offer_eligibility(product, now=now)
        if not offer.eligible:
            return offer
    return Eligibility(True)


def policy_financial_eligibility(
    products: list[Any], *, now: datetime | None = None
) -> Eligibility:
    """Rollout-aware financial gate.

    Known country conflicts and explicit OOS are never financially safe. During
    shadow mode, unknown/stale observations remain visible while coverage is
    measured; enforce mode makes those states fail closed.
    """
    identity = identity_eligibility(products)
    if identity.reason == "country_conflict":
        return identity
    if country_policy_enforced() and not identity.eligible:
        return identity
    for product in products:
        offer = current_offer_eligibility(product, now=now)
        if offer.reason in {"dead_url", "out_of_stock"}:
            return offer
        if availability_policy_enforced() and not offer.eligible:
            return offer
    return Eligibility(True)


def policy_identity_eligibility(products: list[Any]) -> Eligibility:
    identity = identity_eligibility(products)
    if identity.reason == "country_conflict":
        return identity
    if country_policy_enforced() and not identity.eligible:
        return identity
    return Eligibility(True)


def policy_offer_eligibility(product: Any, *, now: datetime | None = None) -> Eligibility:
    offer = current_offer_eligibility(product, now=now)
    if offer.reason in {"dead_url", "out_of_stock"}:
        return offer
    if availability_policy_enforced() and not offer.eligible:
        return offer
    return Eligibility(True)


def current_offer_sql(Product, *, now: datetime | None = None):
    """SQLAlchemy predicate matching ``current_offer_eligibility`` freshness."""
    current = now or utcnow()
    site_terms = [
        and_(
            Product.site == site,
            Product.availability_observed_at >= current - timedelta(hours=max_age),
        )
        for site, max_age in OFFER_MAX_AGE_HOURS.items()
    ]
    return and_(
        Product.url_dead_at.is_(None),
        Product.offer_availability_status == OFFER_IN_STOCK,
        Product.availability_observed_at.is_not(None),
        or_(*site_terms),
    )
