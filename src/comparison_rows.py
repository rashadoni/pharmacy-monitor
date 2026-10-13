"""Строки сравнения цен между сайтами (формат `ComparisonRowOut`).

Собирают их страница сравнения, её поиск и Excel-выгрузка (`src/api.py`) и
утреннее письмо о разнице цен (`src/notifications.py`). Код жил в `src/api.py`
и вынесен оттуда дословно: `src/api.py` при импорте поднимает Sentry, ставит
фильтр журнала доступа и собирает приложение FastAPI, а письмо шлёт команда
CLI, которой веб-приложение импортировать незачем.

`src/api.py` берёт отсюда всё под прежними именами (с подчёркиванием): под ними
их знают его код и тесты. Снаружи модуль зовут по именам без подчёркивания — они
в конце файла.

HTTP здесь нет: отказ «финансовые сравнения приостановлены» (503) остался в
`api._require_financial_policy_ready`. Кто зовёт сборку строк мимо API, сам
спрашивает `product_policy.policy_rollout_eligibility`.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from src import storage
from src._time import utcnow
from src.category_taxonomy import classify_source_category, source_category_labels
from src.normalize import pack_unit_count
from src.product_policy import POLICY_PRODUCT_FIELDS


# Порог свежести цены для comparison (2026-05-29). Считается по
# Product.last_seen_at (обновляется КАЖДЫЙ прогон под diff-only), НЕ по
# snapshot.captured_at (пишется только при смене цены — у товара со стабильной
# ценой captured_at старый, но товар живой). Цена старше порога: показывается
# с бейджем «N дн. назад», но НЕ участвует в расчёте spread/min/max/cheapest,
# чтобы устаревшая цена не давала ложный undercut/spread. Строка всё равно
# отображается (ничего не теряется — клиент видит обе цены + контекст возраста).
_COMPARISON_STALE_DAYS = 14


def _price_age_days(now: datetime, last_seen: datetime | None) -> int | None:
    """Возраст цены в днях по last_seen_at. None → None (last_seen_at=NULL).

    tz-defensive: prod-колонка naive (Postgres timestamp without tz), но если
    прилетит aware datetime — нормализуем (иначе naive-aware вычитание упало бы
    TypeError'ом). Будущие timestamp'ы (clock skew между scraper runtime и API)
    клампятся в 0, чтобы age не уходил в минус.
    """
    if last_seen is None:
        return None
    if last_seen.tzinfo is not None:
        last_seen = last_seen.replace(tzinfo=None)
    return max(0, (now - last_seen).days)


def _comparison_spread(
    prices: dict[str, dict[str, Any]],
) -> tuple[str, float | None, float | None, str | None, float | None]:
    """Per-unit-aware spread для comparison-строки (учёт свежести).

    Мутирует `prices`: (а) добавляет каждому сайту `pack_count` + `unit_price`,
    (б) УДАЛЯЕТ сайты с явно-битой СВЕЖЕЙ ценой (outlier < 12% медианы — это
    parse-ошибка/не та единица, не реальный undercut). Возвращает
    (basis, min, max, cheapest_site, spread_pct) на выбранном basis.

    Свежесть (2026-05-29): записи с `stale=True` (last_seen_at старше
    _COMPARISON_STALE_DAYS) ОСТАЮТСЯ в `prices` для отображения с бейджем, но в
    расчёт basis/outlier/stats НЕ входят — устаревшая цена не должна двигать
    spread/undercut. Если свежих цен < 2 → spread не считается (None), но строка
    всё равно показывается со stale-ценами.

    Логика per-unit (2026-05-29, Perplexity+Codex consensus):
    - Считаем count штук в упаковке + unit_price = price/count.
    - Outlier-фильтр работает на UNIT-цене (не raw): для маски поштучно 0.20
      vs пачки N50 за 10.00 unit price у обоих 0.20 → ни один не выбрасывается
      (старый raw-фильтр ошибочно дропал 0.20 как «<10% от 10»).
    - basis="unit" ТОЛЬКО если фасовки реально различаются (max/min count ≥ 2,
      ≥1 high-confidence) И нормализация УМЕНЬШАЕТ spread. Это снимает риск
      «один сайт N50, другой не распарсился → ложный 50×»: если нормализация
      раздувает spread — это parse-артефакт, остаёмся на raw.
    """
    for data in prices.values():
        cnt, conf = pack_unit_count(data.get("pack_size"), data.get("name"))
        data["pack_count"] = cnt
        data["count_conf"] = conf
        data["unit_price"] = data["price"] / cnt if cnt > 0 else data["price"]

    # Свежие = участвуют в расчёте. Stale остаются в `prices` для показа (бейдж).
    fresh = {s: d for s, d in prices.items() if not d.get("stale")}

    def _spread_over(key: str) -> float:
        vs = [d[key] for d in fresh.values()]
        return (max(vs) - min(vs)) / max(vs) * 100 if vs and max(vs) else 0.0

    # ── Шаг 1: выбрать basis ДО outlier-фильтра (только по свежим) ──
    # (фильтровать надо по той цене, которой доверяем; иначе при недостоверных
    #  count'ах unit-цены ложные и дропнут легитимный сайт — risk #4.)
    basis = "raw"
    if len(fresh) >= 2:
        counts = [d["pack_count"] for d in fresh.values()]
        has_high = any(d["count_conf"] == "high" for d in fresh.values())
        counts_differ = max(counts) / min(counts) >= 2 if min(counts) > 0 else False
        # unit только если фасовки различаются И нормализация УМЕНЬШАЕТ spread.
        if has_high and counts_differ and _spread_over("unit_price") < _spread_over("price"):
            basis = "unit"

    key = "unit_price" if basis == "unit" else "price"

    # ── Шаг 2: СИММЕТРИЧНЫЙ outlier-фильтр по свежим (parse-ошибки/wrong-match) ──
    # Низкий выброс (<12% медианы = >8.3× дешевле): почти всегда parse-ошибка /
    #   не та единица (Thiogamma aloe 8.90 vs pharm 89.00 = 10×).
    # Высокий выброс (>8.3× медианы): ловится ТОЛЬКО при 3+ сайтах, где медиана —
    #   реальный консенсус. Напр. wrong-match «Aspirin C» 8.02 при aptek/aloe
    #   0.30/0.30 → дропаем 8.02, остаётся консенсус 0.30/0.30 (spread 0%).
    #   При 2 сайтах median == max, высокий порог не срабатывает → поведение
    #   2-сайтовых строк без изменений. Реальный 2-5× undercut остаётся виден.
    # Битую ТЕКУЩУЮ цену дропаем из `prices` (не показываем); stale не трогаем.
    if len(fresh) >= 2:
        vals = sorted(d[key] for d in fresh.values())
        median = vals[len(vals) // 2]
        low_t = median * 0.12
        high_t = median / 0.12 if median > 0 else float("inf")
        for site in [s for s, d in fresh.items() if d[key] < low_t or d[key] > high_t]:
            del prices[site]
            del fresh[site]

    # <2 свежих цен (все stale или одна осталась после outlier-дропа) —
    # сравнивать не с чем: возвращаем ВСЁ None. Иначе единственная свежая цена
    # дала бы spread=None, но min/max/cheapest были бы выставлены, и frontend
    # ложно подсветил бы её как «дешёвую» (Codex MED, 2026-05-29). Stale-записи
    # остаются в `prices` для отображения с бейджем.
    if len(fresh) < 2:
        return basis, None, None, None, None

    # ── Шаг 3: stats только по свежим ──
    items = [(s, d[key]) for s, d in fresh.items()]
    mn = min(v for _, v in items)
    mx = max(v for _, v in items)
    cheap = min(items, key=lambda x: x[1])[0]
    spr = round((mx - mn) / mx * 100, 1) if mx else 0.0
    return basis, mn, mx, cheap, spr


# Клиент — первым: это порядок колонок на странице и приоритет при равных ценах.
_SITE_ORDER = {"pharmonline": 0, "aptekonline": 1, "aloe": 2}

# Колонки товара, которые читает сборка строк сравнения. Грузим ровно их и
# без ORM-объектов: в `products` лежат тяжёлые `description`/`normalized_attrs`,
# а строк в полном списке — тысячи на каждый запрос. Поля, нужные политике
# стран и наличия, берём из её собственного списка — см. POLICY_PRODUCT_FIELDS.
_COMPARISON_PRODUCT_COLUMNS = tuple(
    getattr(storage.Product, field)
    for field in dict.fromkeys(
        (
            "id",
            "canonical_id",
            "url",
            "name",
            "category",
            "pack_size",
            "last_seen_at",
            *POLICY_PRODUCT_FIELDS,
        )
    )
)


def _has_ok_run(db: Session, *, tenant_id: int) -> bool:
    return (
        db.scalar(
            select(storage.Run.id)
            .where(storage.Run.status == "ok", storage.Run.tenant_id == tenant_id)
            .limit(1)
        )
        is not None
    )


def _trusted_snapshot_lineage_available(
    db: Session,
    *,
    tenant_id: int = 1,
) -> bool:
    """Whether every required site has a fresh verified full-catalog Run.

    A single site's first financially eligible Run is not a complete price
    lineage. With diff-only persistence that Run can legitimately contain no
    PriceSnapshot rows when prices stayed unchanged. Enabling the eligible-run
    filter at that point would discard the older effective prices for every
    site and turn comparison endpoints into an empty 200 response.

    Shadow mode therefore keeps the latest effective snapshots until a full
    cross-site trusted epoch exists. Enforce mode remains fail-closed in
    ``api._require_financial_policy_ready``.

    Within an epoch an unchanged price is still trusted: the verified run
    confirms the snapshot it compared against, see
    ``storage.trusted_snapshot_filter``.
    """
    from src.product_policy import trusted_catalog_epoch

    return trusted_catalog_epoch(db, tenant_id=tenant_id) is not None


def _comparison_rows(
    db: Session,
    *,
    tenant_id: int,
    min_sites: int = 2,
    site_filter: str | None = None,
    min_confidence: float = 0.70,
    category: str | None = None,
    match_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Строки таблицы сравнения (формат `ComparisonRowOut`), по spread убыв.

    `match_ids` ограничивает выборку конкретными кластерами (поиск); `None` —
    все кластеры арендатора.

    `min_confidence` (default 0.70): отсекаем низко-достоверные fuzzy-матчи —
    они почти всегда РАЗНЫЕ товары (Bio Kolik капли ↔ Bio sprey, Vitamin C
    таблетки ↔ Vitamin C ампула), сматченные по generic-токенам, и дают
    ложный гигантский spread в топе. is_manual=True матчи (подтверждены
    человеком) показываются всегда, независимо от confidence.
    """
    from src.product_policy import policy_identity_eligibility, policy_offer_eligibility

    # Bug fix 2026-05-29: `limit` нельзя ставить на запрос кластеров — сначала
    # фильтр по min_sites и свежести цен, потом сортировка, и только потом срез
    # (иначе при 4824 кластерах было видно 10%). Поэтому здесь читаются ВСЕ
    # кластеры выборки, а limit применяет вызывающий код к готовому списку.
    match_stmt = (
        select(
            storage.Match.id,
            storage.Match.canonical_name,
            storage.Match.canonical_brand,
            storage.Match.canonical_pack_size,
            storage.Match.is_manual,
            storage.Match.confidence,
        )
        .where(storage.Match.tenant_id == tenant_id)
        .order_by(storage.Match.id)
    )
    # Порядок товаров внутри кластера влияет на результат в двух местах, и раньше
    # он был случайным (как вернёт БД):
    #  - при РАВНЫХ ценах `cheapest_site` — первый по порядку; ставим клиента
    #    (pharmonline) первым, чтобы паритет не рисовался как «конкурент дешевле»;
    #  - на одном сайте в кластере иногда два товара (старый дубль), в
    #    `prices[site]` остаётся последний — по id это новейший.
    product_stmt = (
        select(*_COMPARISON_PRODUCT_COLUMNS)
        .where(storage.Product.tenant_id == tenant_id)
        .order_by(
            case(_SITE_ORDER, value=storage.Product.site, else_=len(_SITE_ORDER)),
            storage.Product.id,
        )
    )
    if match_ids is None:
        product_stmt = product_stmt.where(storage.Product.canonical_id.is_not(None))
    else:
        if not match_ids:
            return []
        wanted = sorted(match_ids)
        match_stmt = match_stmt.where(storage.Match.id.in_(wanted))
        product_stmt = product_stmt.where(storage.Product.canonical_id.in_(wanted))
    matches = db.execute(match_stmt).all()
    if not matches:
        return []
    known_match_ids = {m.id for m in matches}
    products_by_match: dict[int, list[Any]] = defaultdict(list)
    all_pids: list[int] = []
    for product in db.execute(product_stmt):
        if product.canonical_id in known_match_ids:
            products_by_match[product.canonical_id].append(product)
            all_pids.append(product.id)

    # Последняя цена на товар одной агрегатной SELECT'ой (после diff-only
    # persist'а 2026-05-09 `WHERE run_id == last_run` пропускал товары без
    # изменения цены в последнем прогоне).
    #
    # Independent site producers reach their first verified full run at
    # different times. A single site's eligible diff-only run is not a complete
    # cross-site snapshot lineage and may contain zero snapshots when no prices
    # changed. Keep the shadow fallback until every required site contributes a
    # fresh verified full run; hard product gates below still exclude unsafe
    # offers, and enforce mode already fails closed in the endpoint.
    trusted_lineage_available = _trusted_snapshot_lineage_available(db, tenant_id=tenant_id)
    prices_by_pid = storage.latest_prices_per_product(
        db,
        all_pids,
        financially_eligible_only=trusted_lineage_available,
        tenant_id=tenant_id,
    )

    now = utcnow()  # naive UTC; last_seen_at тоже naive (src/_time) — вычитание ок
    # Ярлыки тянем ОДИН раз до цикла: source_category_labels сканирует Category
    # целиком, внутри цикла это был бы скан на каждый матч.
    source_labels = source_category_labels(db) if category is not None else {}
    # И мемоизируем классификацию по категории-источнику: order-independent
    # классификатор прогоняет ВСЕ ~200 правил (в этом и смысл), поэтому вызов
    # на каждый матч — это ~200 правил × ~5k матчей регэкспов на запрос вместо
    # ~200 × ~178 категорий. Тот же приём, что в analytics.category_comparison.
    _canonical_cache: dict[str, str | None] = {}

    def _canonical_key_for(raw_category: str | None) -> str | None:
        cache_key = raw_category or ""
        if cache_key not in _canonical_cache:
            label_ru, label_az = source_labels.get(("pharmonline", cache_key), (None, None))
            resolved = classify_source_category(
                "pharmonline", raw_category, label_ru=label_ru, label_az=label_az
            )
            _canonical_cache[cache_key] = resolved.key if resolved else None
        return _canonical_cache[cache_key]

    out: list[dict[str, Any]] = []
    for m in matches:
        products = products_by_match.get(m.id, [])
        if not policy_identity_eligibility(products).eligible:
            continue
        # Drill-down из /category-comparison: фильтр по категории товара-клиента
        # (pharmonline). None → без фильтра (обычный режим страницы сравнения).
        if category is not None:
            client_p = next((p for p in products if p.site == "pharmonline"), None)
            if client_p is None:
                continue
            # Классифицируем ровно так же, как сводка /category-comparison —
            # со слагом И ярлыками. Классификация только по слагу расходилась бы
            # со сводкой: строка есть в сводке, а drill-down пуст.
            canonical_key = _canonical_key_for(client_p.category)
            if client_p.category != category and canonical_key != category:
                continue
        # Confidence floor: низко-достоверные fuzzy-матчи (Bio Kolik ↔ Bio sprey)
        # дают ложный spread. Ручные (is_manual) показываем всегда.
        conf = m.confidence if m.confidence is not None else 1.0
        if not m.is_manual and conf < min_confidence:
            continue
        # Если товар-КЛИЕНТ (pharmonline) мёртв (404) — матч бесполезен (нечего
        # сравнивать с ценой клиента), дропаем целиком, а не показываем competitor-only
        # строку (аудит M1; зеркалит analytics._iter_matched_prices).
        if any(p.site == "pharmonline" and p.url_dead_at is not None for p in products):
            continue
        prices: dict[str, dict[str, Any]] = {}
        for p in products:
            # «Фантомные» товары (страница 404, помечены validate-links) — скрываем,
            # чтобы не показывать матч с мёртвой ссылкой на конкурента.
            if p.url_dead_at is not None:
                continue
            if not policy_offer_eligibility(p, now=now).eligible:
                continue
            latest = prices_by_pid.get(p.id)
            price = (latest[1] or latest[0]) if latest else None
            if price is not None and price > 0:
                # Свежесть по last_seen_at (видели в прогоне), НЕ по captured_at:
                # под diff-only стабильная цена имеет старый captured_at, но свежий
                # last_seen_at — товар активен. last_seen_at=NULL → не stale.
                age_days = _price_age_days(now, p.last_seen_at)
                prices[p.site] = {
                    "price": price,
                    "is_on_sale": latest[2] if latest else False,
                    "url": p.url,
                    "product_id": p.id,
                    "country_code": p.manufacturer_country_code,
                    "country_resolution_status": p.country_resolution_status,
                    "availability_status": p.offer_availability_status,
                    "availability_observed_at": p.availability_observed_at,
                    # pack_size + name нужны для per-unit нормализации (ниже)
                    "pack_size": p.pack_size,
                    "name": p.name,
                    "age_days": age_days,
                    "stale": age_days is not None and age_days > _COMPARISON_STALE_DAYS,
                }

        # Per-unit-aware spread + outlier-фильтр (2026-05-29). Заменяет прежний
        # raw-price 10%-median фильтр (он ошибочно дропал легитимную цену-за-
        # штуку). `_comparison_spread` нормализует цену за штуку когда фасовки
        # различаются и это уменьшает spread, и дропает parse-ошибки на unit-цене.
        # Мутирует prices (annotate + drop).
        basis, min_p, max_p, cheapest, spread = _comparison_spread(prices)

        sites_with_price = len(prices)
        if sites_with_price < min_sites:
            continue
        if site_filter and site_filter not in prices:
            continue
        # Убираем служебные поля из payload (оставляем pack_size/count/unit_price).
        for d in prices.values():
            d.pop("name", None)
            d.pop("count_conf", None)
        out.append(
            {
                "canonical_id": m.id,
                "name": m.canonical_name,
                "brand": m.canonical_brand,
                "pack_size": m.canonical_pack_size,
                "is_manual": bool(m.is_manual),
                "sites_with_price": sites_with_price,
                "min_price": min_p,
                "max_price": max_p,
                "spread_pct": spread,
                "cheapest_site": cheapest,
                "prices": prices,
                "confidence": conf,
                "needs_review": spread is not None and spread >= 50.0,
                "spread_basis": basis,
            }
        )
    # Сортируем по |spread| desc (самое полезное для PO — где конкурент бьёт
    # по цене / где можно поднять).
    out.sort(key=lambda r: r["spread_pct"] if r["spread_pct"] is not None else -1.0, reverse=True)
    return out


# Имена для тех, кто зовёт модуль снаружи. Это те же функции, не обёртки.
comparison_rows = _comparison_rows
has_ok_run = _has_ok_run
