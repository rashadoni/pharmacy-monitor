# Incremental Scrape — Architecture Research (2026-05-09)

## Вопрос
Можно ли отказаться от full daily scrape всех 100K продуктов и обновлять
только то, что реально изменилось?

## Что проверено

### aptekonline.az
- **JSON API** `/shop/productList?categoryId[]=N&page=N` — Laravel paginator,
  whitelist параметров: только `categoryId[]`, `lang`, `page`. Любые
  `orderBy`, `sort=updated_at`, `since=...` → HTTP 302 redirect на `/`.
- **Поля продукта**: `name, name_ru, terkib, vahid, olke, price,
  discount_price, thumb1, url_id, ededsay, qadaga, qaliq, status, categories`.
  **Нет `updated_at`, `created_at`, `last_modified`.**
- **`thumb1`** (e.g. `thumb_66960_774.jpg`) — внутренний ID файла фотографии,
  не sequential product ID. Примеры из одной категории: 26442, 46797, 66960,
  150852, 161270 — не отсортированы и не растут монотонно.
- **`/sitemap.xml`** возвращает пустой `<urlset></urlset>` (HTTP 200, 283 байта).

**Вердикт**: невозможно отличить "обновлённый" товар от "не изменившегося"
без полного fetch'а каждой страницы / API-вызова.

### pharmonline.az
- **`/sitemap.xml`** возвращает sitemap-INDEX → 20+ sub-sitemap'ов
  (`sitemap-products-1.xml` ... `sitemap-products-N.xml`), 990 URL'ов в каждом.
- Каждый product URL имеет `<lastmod>` И `<changefreq>daily</changefreq>`.
- **Проблема**: `<lastmod>` одинаковый для ВСЕХ 990 URL'ов в sub-sitemap'е
  (`2026-05-09T05:54:44+00:00`). Это таймстамп **перегенерации sitemap'а**,
  а не изменения конкретного товара. `<changefreq>daily</changefreq>` —
  декларация сайта, не реальный flag.
- Скрейпер уже использует category-pages (а не product-pages) — это
  оптимально по network для текущего подхода.

**Вердикт**: sitemap бесполезен для per-product delta. Push-driven невозможен.

### aloe.az
- **`/sitemap.xml`** содержит 19307 URL'ов, каждый с `<lastmod>`.
- **Только 55 уникальных значений `<lastmod>`** на 19307 URL'ов — большинство
  из 2025-06 / 2025-07 / 2025-12 (даты импорта/публикации товаров).
- Только 1-2 lastmod указывают на сегодняшнюю дату — для homepage и
  promo-страниц, не для отдельных товаров.
- Цены товаров на aloe могут меняться без обновления lastmod в sitemap'е.

**Вердикт**: lastmod показывает дату создания страницы, не изменения цены.
Бесполезен для daily delta.

## Что реально работоспособно

**Option 1 + Option 2 hybrid: smart full-scan + diff-only persist**.

Сама фаза scrape остаётся как сегодня (это уже эффективно, особенно для
aptekonline JSON API ~3-5 мин на 90K продуктов). Но:

1. **persist_results** меняется так:
   - Получили `ScrapedProduct(price=X)` для existing product_id=42.
   - SELECT latest snapshot for product_id=42 (один LEFT JOIN на чанк, не N+1).
   - Если `latest_snapshot.price == X AND latest_snapshot.discount_price == Y`
     → НЕ INSERT'ить новый snapshot. Только `UPDATE products.last_seen_at`.
   - Иначе INSERT новый snapshot.

2. **Эффект на нагрузку**:
   - Сегодня: 100K snapshots/день, 100K rows в `price_snapshots`.
   - С diff-only: ожидаемо ~2-5K snapshots/день (pharm. цены статичны).
   - 30-50× меньше I/O в `price_snapshots`, persist через SSH-tunnel
     становится секундами.

3. **Watchlist priority** (ортогонально, сделать вторым шагом):
   - Pinned items в watchlist: всегда полный scrape, независимо от расписания.
   - Категории с высокой волатильностью (косметика, детское питание) —
     каждый день. Стабильные (гомеопатия) — раз в неделю.

## Что НЕ работает / не рекомендуется

- **Не пытаться** реализовать "push-driven" (lastmod-based) — sitemap'ы
  лгут или пусты, подтверждено эмпирически.
- **Не парсить отдельно `recently updated`** — таких endpoint'ов нет.
- **Не оптимизировать фазу scrape для aptekonline** — JSON API уже
  ~3-5 мин на 90K продуктов, упирается в pagination, а не в наш код.

## Следующие шаги

1. **Diff-only persist** (~3-4 часа кода + тесты + деплой) — даст основной
   выигрыш. См. план в этом доке выше.
2. **Watchlist priority queue** (~1 день) — для важных SKU.
3. **Smart sampling по категориям** (~2-3 дня) — последний штрих.

Source файлы для diff-only:
- `src/main.py:persist_results` — добавить SELECT latest snapshot per
  product перед INSERT'ом.
- `src/storage.py` — возможно, добавить index `(product_id, captured_at DESC)`
  для быстрого latest lookup, если ещё нет.
- `tests/test_persist_results.py` — добавить тесты diff-only поведения.
