from datetime import timedelta

from src._time import utcnow
from src.product_observations import apply_product_observation
from src.product_policy import (
    COUNTRY_AMBIGUOUS,
    COUNTRY_INVALID,
    COUNTRY_RESOLVED,
    OFFER_IN_STOCK,
    OFFER_OUT_OF_STOCK,
    country_resolution,
    financially_eligible,
    identity_eligibility,
    normalize_country_code,
    offer_from_quantity,
)
from src.scrapers.base import ScrapedProduct
from src.storage import Product


def _product(site: str = "aloe") -> Product:
    return Product(
        id=1,
        tenant_id=1,
        site=site,
        external_id="x",
        url="https://example/x",
        name="X",
        name_normalized="x",
    )


def _scraped(**kwargs) -> ScrapedProduct:
    return ScrapedProduct(
        site="aloe",
        external_id="x",
        url="https://example/x",
        name="X",
        **kwargs,
    )


def test_country_normalization_handles_client_examples() -> None:
    assert normalize_country_code("Украина") == "ua"
    assert normalize_country_code("Сербия") == "rs"
    assert normalize_country_code("Англия") == "gb"
    assert normalize_country_code("Latviya") == "lv"
    assert country_resolution("14") == (None, COUNTRY_AMBIGUOUS)
    assert normalize_country_code("TUR") == "tr"
    assert normalize_country_code("GBR") == "gb"
    assert normalize_country_code("Birləşmiş Krallıq") == "gb"
    assert normalize_country_code("BƏƏ") == "ae"


def test_country_normalization_rejects_non_iso_two_letter_noise() -> None:
    assert normalize_country_code("RS") == "rs"
    assert normalize_country_code(" ua ") == "ua"
    assert normalize_country_code("XX") is None
    assert normalize_country_code("AB") is None
    assert country_resolution("zz") == (None, "invalid")


def test_country_normalization_handles_aloe_card_spellings() -> None:
    """Написания, которыми aloe.az подписывает страну на карточке (замер 2026-10-07).

    Пока словарь их не знал, числовой id страны из листинга оставался
    неразрешённым, и гард сопоставления такую страну не видел.
    """
    observed = {
        "Израиль": "il",
        "Британия": "gb",
        "Britain": "gb",
        "Шотландия": "gb",
        "Fransiya": "fr",
        "ЮАР": "za",
        "Южная Корея": "kr",
        "Бангладеш": "bd",
        "Rumıniya": "ro",
        "Австралия": "au",
        "Niderlandiya": "nl",
        "Голландия": "nl",
        "Пуерто-Рико": "pr",
        "Саудовская-Арабия": "sa",
        "Иордания": "jo",
        "Канада": "ca",
        "Уругвай": "uy",
        "Малайзия": "my",
        "Словакия": "sk",
        "Кипр": "cy",
        "Оман": "om",
        "Черногория": "me",
        "Босния": "ba",
        "Таиланд": "th",
        "Бразилия": "br",
        "Мальта": "mt",
        "Туркменистан": "tm",
        "Белоруссия": "by",
        "Индонезия": "id",
        "Филлипины": "ph",
        "Мексика": "mx",
        "Перу": "pe",
        "Сингапур": "sg",
        "Колумбия": "co",
        "Агрентина": "ar",
        # Уже лежали в aloe_country_mappings, но словарём не разрешались.
        "Argentina": "ar",
        "Аргентина": "ar",
        "Ирландия": "ie",
        "Сан-Марино": "sm",
    }
    for raw, code in observed.items():
        assert country_resolution(raw) == (code, COUNTRY_RESOLVED), raw


def test_country_normalization_keeps_ambiguous_aloe_labels_unresolved() -> None:
    """Страновая политика не ослабляется ради процента покрытия.

    Две страны сразу, город, название фирмы, непонятное сокращение и заглушка
    сайта — не страна происхождения. Такое значение должно остаться
    неразрешённым, иначе гард разведёт пары по выдуманному признаку.
    """
    for raw in (
        "Türkiyə-Almaniya",
        "Турция-Гер",
        "Курган",
        "Санкт-Пете",
        "Специфарма",
        "НВ",
        "Country",
    ):
        assert normalize_country_code(raw) is None, raw
        assert country_resolution(raw) == (None, COUNTRY_INVALID), raw


def test_country_dictionary_spellings_survive_key_folding() -> None:
    """Каждое написание из словаря находится после свёртки ключа.

    Регрессия: «китай», «швейцария», «азербайджан» и «rumıniya» лежали в
    словаре буквально, а входное значение сворачивалось (й→и, ı→i) — ключ не
    совпадал, и страна молча оставалась неразрешённой.
    """
    from src.product_policy import (
        _COUNTRY_ALIASES,
        _COUNTRY_SPELLINGS,
        _ISO_ALPHA2,
        _country_key,
    )

    folded: dict[str, str] = {}
    for spelling, code in _COUNTRY_SPELLINGS.items():
        assert code in _ISO_ALPHA2, spelling
        key = _country_key(spelling)
        # Два написания не должны сворачиваться в один ключ с разными странами.
        assert folded.setdefault(key, code) == code, spelling
        assert _COUNTRY_ALIASES[key] == code, spelling

    assert normalize_country_code("Китай") == "cn"
    assert normalize_country_code("Швейцария") == "ch"
    assert normalize_country_code("Азербайджан") == "az"
    assert normalize_country_code("RUMINİYA") == "ro"


def test_quantity_is_tri_state_not_missing_equals_zero() -> None:
    assert offer_from_quantity(None) == ("unknown", None)
    assert offer_from_quantity(0) == (OFFER_OUT_OF_STOCK, 0.0)
    assert offer_from_quantity("2") == (OFFER_IN_STOCK, 2.0)


def test_country_change_requires_two_distinct_runs() -> None:
    now = utcnow()
    product = _product()
    first = _scraped(
        manufacturer_country_raw="Украина",
        country_source="detail",
    )
    apply_product_observation(product, first, run_id=10, observed_at=now)
    assert product.manufacturer_country_code == "ua"
    assert product.country_resolution_status == COUNTRY_RESOLVED

    changed = _scraped(
        manufacturer_country_raw="Сербия",
        country_source="detail",
    )
    apply_product_observation(product, changed, run_id=11, observed_at=now)
    assert product.manufacturer_country_code == "ua"
    assert product.country_candidate_code == "rs"
    assert product.country_resolution_status == COUNTRY_AMBIGUOUS

    # Duplicate observation in the same run cannot promote identity.
    apply_product_observation(product, changed, run_id=11, observed_at=now)
    assert product.manufacturer_country_code == "ua"
    assert product.country_candidate_seen_count == 1

    apply_product_observation(product, changed, run_id=12, observed_at=now)
    assert product.manufacturer_country_code == "rs"
    assert product.country_resolution_status == COUNTRY_RESOLVED
    assert product.country_candidate_code is None


def test_unknown_signal_never_erases_resolved_country_or_offer() -> None:
    now = utcnow()
    product = _product()
    known = _scraped(
        manufacturer_country_raw="Italy",
        country_source="detail",
        offer_availability_status=OFFER_IN_STOCK,
        offer_quantity=3,
        availability_source="quantity",
    )
    apply_product_observation(product, known, run_id=1, observed_at=now)
    unknown = _scraped(
        manufacturer_country_raw="14",
        country_source="listing_id",
    )
    apply_product_observation(product, unknown, run_id=2, observed_at=now)

    assert product.manufacturer_country_code == "it"
    assert product.country_resolution_status == COUNTRY_RESOLVED
    assert product.offer_availability_status == OFFER_IN_STOCK
    assert product.offer_quantity == 3


def test_financial_eligibility_requires_same_country_and_fresh_stock() -> None:
    now = utcnow()
    a, b = _product("aloe"), _product("pharmonline")
    for product in (a, b):
        product.manufacturer_country_code = "rs"
        product.country_resolution_status = COUNTRY_RESOLVED
        product.offer_availability_status = OFFER_IN_STOCK
        product.availability_observed_at = now
    assert financially_eligible([a, b], now=now).eligible is True

    b.manufacturer_country_code = "ua"
    assert financially_eligible([a, b], now=now).reason == "country_conflict"
    b.manufacturer_country_code = "rs"
    b.availability_observed_at = now - timedelta(days=20)
    assert financially_eligible([a, b], now=now).reason == "availability_stale"


def test_unknown_country_does_not_mask_two_resolved_country_conflicts() -> None:
    ua = _product("aloe")
    rs = _product("aptekonline")
    unknown = _product("pharmonline")
    ua.manufacturer_country_code = "ua"
    ua.country_resolution_status = COUNTRY_RESOLVED
    rs.manufacturer_country_code = "rs"
    rs.country_resolution_status = COUNTRY_RESOLVED

    assert identity_eligibility([ua, rs, unknown]).reason == "country_conflict"


def test_offer_freshness_window_follows_site_cadence():
    """Окно свежести оффера выводится из ритма сбора, а не из общих 30ч.

    aptekonline собирается раз в неделю (решение владельца 2026-10-04), поэтому
    самое свежее наблюдение по нему физически бывает недельной давности. Прежние
    жёсткие 30ч объявляли его собственный успешный сбор несвежим уже через
    полтора дня — полный каталог не подтверждался 6 дней из 7.
    """
    from src.cadence import site_max_age_hours
    from src.product_policy import OFFER_MAX_AGE_HOURS

    # Все три сайта на недельном ритме (решение владельца 2026-10-06).
    for site in ("pharmonline", "aptekonline", "aloe"):
        assert OFFER_MAX_AGE_HOURS[site] == 174

    # Формула не ломает суточный случай: сайт без объявленного ритма — 30ч.
    assert site_max_age_hours("site-without-declared-cadence") == 30


def test_weekly_site_offer_stays_fresh_mid_cycle():
    """Наблюдение на 100ч — середина штатного недельного цикла у любого сайта."""
    from src.product_policy import offer_is_fresh

    now = utcnow()
    for site in ("pharmonline", "aptekonline", "aloe"):
        product = _product(site)
        product.availability_observed_at = now - timedelta(hours=100)
        assert offer_is_fresh(product, now=now) is True, site


def test_weekly_site_offer_goes_stale_after_missed_scan():
    """Пропущенный недельный сбор (>174ч) по-прежнему делает оффер несвежим."""
    from src.product_policy import offer_is_fresh

    now = utcnow()
    for site in ("pharmonline", "aptekonline", "aloe"):
        product = _product(site)
        product.availability_observed_at = now - timedelta(hours=200)
        assert offer_is_fresh(product, now=now) is False, site


def test_full_verification_reason_prefers_untruncated_run_quality():
    """Причина отказа читается целиком, а не обрезанной до varchar(300).

    Регрессия по реальному кейсу: у pharmonline строка обрывалась на
    `catalog_floor_missing` без значения, и это читалось как отказ из-за
    catalog_floor, хотя настоящая причина — `missing_trusted_ids=15`, а
    различающие счётчики (mismatched_urls, id_collisions, url_collisions)
    в колонку вообще не помещались.
    """
    from types import SimpleNamespace

    from src.product_policy import _full_verification_reason

    full = "public_api_identity_proof_failed:" + "detail=1, " * 40
    run = SimpleNamespace(
        run_quality={"catalog_verification_reason_full": full},
        catalog_verification_reason=full[:300],
    )
    assert _full_verification_reason(run) == full
    assert len(_full_verification_reason(run)) > 300


def test_full_verification_reason_falls_back_to_column():
    """Старые прогоны без run_quality по-прежнему отдают, что есть в колонке."""
    from types import SimpleNamespace

    from src.product_policy import _full_verification_reason

    assert (
        _full_verification_reason(
            SimpleNamespace(run_quality=None, catalog_verification_reason="legacy_reason")
        )
        == "legacy_reason"
    )
    assert (
        _full_verification_reason(
            SimpleNamespace(run_quality={}, catalog_verification_reason="legacy_reason")
        )
        == "legacy_reason"
    )
