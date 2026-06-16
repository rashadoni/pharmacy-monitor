# Pharmacy Monitor — Working Context for Claude

This file is auto-loaded in every Claude Code session. Read it first.

## Verification Protocol (CRITICAL)

Before starting any task that involves code changes, you MUST:

1. **Analyze the request** and provide a brief summary of what you understood.
2. **Define a Verification Plan**: List a specific checklist of how you (and the user) will verify the changes.
3. **Wait for implicit or explicit confirmation** (or proceed if the plan is exhaustive).

If you cannot define a clear checklist for verification, **stop and ask for clarification**. Inability to define a checklist is a signal that the task requirements are ambiguous.

### Checklist Requirements

The Verification Plan must enumerate:

- **Unit tests** to run or write (e.g., `pytest tests/test_<module>.py -v`).
- **Manual verification steps** the user can perform (e.g., "open https://leaddrive.cloud/comparison and check prices show on all 3 sites").
- **Edge cases** to consider (e.g., empty input, network failures, concurrent runs, diff-only vs full-persist data).
- **Production impact** if any (DB migrations, systemd restarts, schema changes).
- **Rollback plan** for irreversible operations.

### When to skip

This protocol applies to tasks that change code. Trivial read-only operations (status checks, file reads, exploration) don't need a Verification Plan. Trivial doc updates (typos, minor wording) don't need one either, but anything affecting prod systems or business logic does.

### Visual verification — no screenshot lies

When confirming a UI change via screenshot (browser MCP, computer-use, screenshots, photos):

- **NEVER** assert «вот, X виден» without identifiable evidence — name the exact pixel/glyph/coordinate and describe what makes it identifiable
- If the element is <16px or visually ambiguous — **zoom tighter** before asserting
- Cross-check via accessibility tree: `find` returns `title` / `aria-label` attributes that survive in the DOM even when the visual glyph is too small to read
- If still unsure — say «не вижу — проверю иначе», NEVER produce a confident-but-wrong claim
- If a marker reads ambiguously at small sizes — **fix the rendering** (filled dot + ring beats a 9px emoji), do not claim it's «already visible»
- **Reason**: 2026-05-27 in another project Claude saw a 1-letter macro provenance marker and confidently called it the new ⏳ hourglass marker. User trusted the false positive until manually zooming and discovering nothing was there. False positives erode trust faster than missing features. A confident wrong answer is worse than «I can't clearly see X».

## Current production state (last updated: 2026-05-27)

**Live URL**: https://leaddrive.cloud (also www.leaddrive.cloud) — TLS via Let's Encrypt, auto-renew (cert valid until 2026-08-04)
**Login**: `admin` / `pharmacy2026`
**Server**: Hetzner cx33 (Falkenstein DE), 4 vCPU / 8GB / 80GB · €7.99/mo · IP `46.225.149.52`
**SSH**: `ssh -i ~/.ssh/id_ed25519 root@46.225.149.52` (root + pm users active; pm home is `/opt/pharmacy-monitor`, NOT `/home/pm`; `pm` does not have passwordless sudo — use root for systemctl/sudo ops)
**DNS**: leaddrive.cloud at Namecheap; A `@` and `www` → 46.225.149.52

### What's running
| Service | Status | Notes |
|---|---|---|
| Caddy :80/:443 | active | auto-HTTPS, HSTS preload, reverse proxy → /api/\* + /docs + /openapi.json + /auth/\* → :8080, else → :3000. `admin off` set → use `systemctl restart caddy` (reload fails). |
| FastAPI :8080 | active | uvicorn 2 workers, JWT auth via password endpoint |
| Next.js :3000 | active | standalone build at `/opt/pharmacy-monitor/frontend/.next/standalone` |
| Postgres 16 | active | DB `pharmacy_monitor`, user `pm` |
| Redis 7 | active | cache + future rate-limit |
| systemd timers | enabled | scrape@pharmonline 01:00, @aptekonline 02:00, @aloe 03:00, health hourly, backup 04:00 |

### Auth
- Password login via `POST /auth/login {login, password}` → JWT cookie `pm_session` (httpOnly)
- bcrypt hash stored in `/etc/pharmacy-monitor/env` as `ADMIN_PASSWORD_HASH`
- Magic-link flow (`/auth/request` + `/auth/verify`) still works as alternative

### Data
- Pilot data migrated: 2087 products, 83 cross-site matches, 14469 price snapshots, 357 categories
- Manual scrape works: `pharmacy-monitor run --mode category --category-id N`
- Auto cron-scrape starts daily 01:00 UTC

### Local secrets backup (NOT in git)
`data/.prod-secrets-DO-NOT-COMMIT` — has PG_PASS, JWT_SECRET, API_KEY (mode 0600)

## Active work-in-progress

**⚠️ POLICY (client, 2026-05-31): COMMODITY identity = brand + volume + number + COUNTRY — ВСЁ совпадает.** Для масел/чаёв/экстрактов (`is_commodity_name`) разная страна происхождения ИЛИ grade (kosmetik) = РАЗНЫЙ товар, даже при одном бренде (Medoil Türkiyə ≠ Medoil Azərbaycan). Enforced: `matcher._has_conflicting_origin_or_grade` в `_hard_conflict` + `_pairwise_spec_conflict` (revalidate каждый скрейп). Trade-name препараты НЕ затронуты (commodity-gated — одно лекарство выпускается на разных заводах). Чистка прода (2026-05-31): ~51 неидентичный коммодити-кластер разъединён (202→152), 0 неидентичных осталось (revalidate=0). Скрипты: `scripts/diag_commodity_conflicts.py --strict --apply`, `scripts/backfill_brand_from_name.py`. НЕ ослаблять без явного запроса клиента.

**Just finished (2026-06-16, ещё — алерт на пустой прогон + категория aloe `bad`):**
- **`_check_site_zero_scrape`** ([src/health.py](src/health.py)): hourly health-check теперь шлёт CRITICAL «сайт собрал 0 товаров» сразу (в течение часа), не дожидаясь суточного `site_silent`. **Корень молчания:** `_smoke_test_per_site_coverage` ([main.py](src/main.py):393) имел `if current == 0: continue` — худший случай (сайт лёг, 0) был единственным, что НЕ алертило; `empty_run` же маскировался hourly intraday-прогоном другого сайта. Новый чек: порог ровно `==0` (intraday-блипы 100-250 НЕ триггерят), берёт последний ЗАВЕРШЁННЫЙ прогон С ЭТИМ сайтом в `products_per_site` (intraday другого сайта не маскирует), limit(500) + site_silent-бэкстоп. +5 тестов (вкл. точную регрессию + running-skip). **Architect APPROVE.**
- **Диагноз исходного кейса (run_230, aloe 06-11 03:00 = 0):** сам aloe.az вернул HTTP 502 (сайт лежал), на след. день сам восстановился (1866). Не наш баг — но мониторинг это проглядел (закрыто чеком выше).
- **aloe-категория `bad`** — НЕ мусор: азерб. «BAD» = БАД/supplements, 143 товара. Поправлены 3 label-заглушки `label_ru='Ещё'` → **Лекарства / БАД (добавки) / Детский мир** (categories id 355/356/357, прод-БД).

**Just finished (2026-06-16, позже — управление пользователями): admin-страница «Пользователи и роли» + фикс email админа.** Клиент запустил прод, спросил где регулировать суперадмина/роли/дайджест/личные кабинеты юзеров.
- **Диагноз**: бэкенд CRUD получателей УЖЕ был ([api.py](src/api.py) `/api/v1/dash/recipients` GET/POST/PATCH/DELETE, admin-only, self-lockout-гарды) + api-клиент ([api.ts](frontend/src/lib/api.ts) `recipientCreate/Update/Delete`) — не было только UI-страницы.
- **(а) Фикс email админа**: `tenant_users` id1 + `EMAIL_TO` были `rashadrahim·soy·@gmail.com` (опечатка) → исправлены на `…ov`. Логин по общему паролю email-независим, magic-token хранится по row-id (не по email) → без риска lockout (проверено архитектором).
- **(г) Новая страница** [settings/users/page.tsx](frontend/src/app/[locale]/(dashboard)/settings/users/page.tsx) (admin-only, как `/settings/pricing`): список юзеров (card-per-user, мобайл), добавление по email, смена роли admin/viewer, тумблеры daily/weekly дайджест + active + severity, soft-delete. Ссылка-карточка в `/settings` (только для admin). Роут добавлен в `LEGACY_ROUTES` (next.config). i18n ru/az/en (namespace `users`, 32 ключа, сверены node-скриптом).
- **(б) Управление юзерами/ролями** — теперь self-serve через эту страницу. **(в) Периодичность дайджеста** — per-user тумблеры + нота с реальным расписанием (daily 05:00 UTC=09:00 Баку, weekly Пн 06:00 UTC=10:00 Баку; systemd-таймеры).
- **Бэкенд-гард `_is_last_active_admin`** (architect #3): PATCH/DELETE блокируют (400) демоут/удаление ПОСЛЕДНЕГО активного админа (достижимо только в edge: деактивированный админ с живым JWT; для нормального актора недостижимо — он сам активный админ → НЕ переблокирует легит-демоут). +6 тестов (helper, role-gate, create+normalize, self-guards ×2, no-over-block). **Двойной architect-review APPROVE-WITH-NITS** → все NITs (фронт `["me"]`-инвалидация + error/empty-состояния, бэкенд last-admin гард) исправлены.
- Задеплоено: rsync api.py + frontend/src + messages → `pnpm build` (pm) → restart api+frontend. Verified: build ок, `/ru/settings/users` 307 (auth-gate, не 404/500), `/health` 200, 6 тестов зелёные. **NB**: viewer (`rashad.aliyev@zeytunpharma.az`) — только чтение, правки дают 403. **Гоча rsync**: путь с `[locale]`/`(dashboard)` ломает rsync (скобки=glob) → синкать `frontend/src/` целиком (рекурсия в скобки идёт внутри rsync).
- **В git НЕ коммичено** (деплой через rsync); health-фикс этого же дня закоммичен отдельно (`28a4590`).

**Just finished (2026-06-16): pharmonline «49% покрытие» — ДИАГНОЗ = дубли от двух скрейперов (НЕ gap/делистинг) + freshness/coverage-aware site_drop + чистка 9478 дублей.** Клиент: проверить aloe-direct, разобраться с «пропавшими» ~9860 pharmonline, сделать метрику покрытия честной.
- **aloe-direct восстановлен** (был сломан на Decodo 4%): `DECODO_SITES=aptekonline` (aloe откатан на direct), run_276 `run_ok products=1866`, 1782/1809 свежих.
- **Корень «49%»**: pharmonline скрейпился ДВУМЯ путями с РАЗНЫМ ключом `external_id` — сервер-DDP (`_id`, 17-симв. Meteor-хеш, [pharmonline_ddp.py](src/scrapers/pharmonline_ddp.py):501) и Mac-Playwright (URL-слаг, [pharmonline.py](src/scrapers/pharmonline.py) `_external_id_from_href`). Каждый товар = 2 ряда. pharmonline ушёл на сервер-DDP-only → 9860 слаг-рядов осиротели (0 свежих). Каталог раздулся 9840→19811 → site_drop читал 9840/19811=**49% (ложь)**. Доказано: 9784/9971 слаг-рядов делят url со свежим hash-рядом, 9703/9971 — имя. **НЕ scrape-gap, НЕ делистинг** (run_274 покрыл 205/205 категорий failures=0; distinct за 9д = 10296/10333). Mac launchd уже НЕ гоняет pharmonline (`DEFAULT_ARGS=aptekonline`, агент не загружен) → рецидива нет.
- **Чистка** [scripts/dedup_pharmonline_slug_dupes.sql](scripts/dedup_pharmonline_slug_dupes.sql): транзакц., удалила **9478** подтверждённых дублей (8579 бесхозных + 899 с ПЕРЕНОСОМ членства кластера на свежий twin), оставила 388 (305 конфликт-кластер + 83 без twin — саморассосутся через окно свежести). **0 кластеров потеряли pharmonline** (инвариант проверен на проде). Бэкап `pharmonline_slug_dedup_bak_20260616` (9860) + `..._rej_bak_*` (102) — откат. Защита: `LOCK TABLE … SHARE ROW EXCLUSIVE` + live-guard `canonical_id IS NULL` + ассерт-предохранитель (RAISE→ROLLBACK при нарушении). Ревью: Claude-агент (live-валидация, APPROVE-WITH-NITS) + **Codex поймал гонку по `canonical_id`** → добавлен lock+guard. pharmonline total 19811→**10333** (9951 hash + 382 остаток).
- **Метрика site_drop переделана** ([src/health.py](src/health.py) `_check_site_drops`): (1) **знаменатель** = ЖИВОЙ каталог за `_SITE_FRESHNESS_DAYS` (21/21/10д) — не весь накопленный total (исключает будущие осиротевшие/делистнутые ряды); (2) **числитель** = distinct за окно ПОКРЫТИЯ `_SITE_COVERAGE_DAYS` (**14/14/4д** — >недельной каденции + запас), а НЕ за буквальный последний прогон — последним бывает ЧАСТИЧНЫЙ intraday-прогон (run_277=127 товаров → давал ложный 127/10333=1% critical). Итог: pharmonline **~99% → health-check `✓ OK`**.
- **Архитектурное ревью (APPROVE-WITH-NITS) → доп. фиксы**: (#1 HIGH) **pharmonline добавлен в `_SITE_MAX_AGE_HOURS`=198ч** (недельный таймер; иначе default 26ч давал бы ложный site_silent 6/7 дней как только Mac-DR отключат); (#2 MED) окно покрытия расширено 9→14д (2д запаса к 7д каденции было мало); убран мёртвый `history_days`. **16/16 health-тестов** (+партиал-устойчивость, +терпимость к опоздавшему недельному прогону, +орфан-исключение). Задеплоено (rsync + api restart).
- **Follow-up (архитектор #6, ~через 3 недели)**: 382 слаг-остатка (305 конфликт-кластер + 77 no-twin) пока в окне свежести 21д (раздувают знаменатель на ~3.7%, безвредно) → должны состариться к ~07-07 (DDP их не освежает). Проверить: `SELECT count(*) FROM products WHERE site='pharmonline' AND external_id !~ '^[A-Za-z0-9]{17}$' AND last_seen_at >= now()-interval '21 days'` → тренд к 0. **Durable-фикс на будущее**: если когда-либо снова включат полный Playwright-прогон pharmonline — он опять наплодит слаг-дубли; правильный ключ persist'а = Meteor `_id` независимо от пути скрейпа.
- **Открыто (действие клиента — платёж/KYC, НЕ моё)**: Decodo авто-топап + ID-верификация аккаунта (для тяжёлого pharmonline-трафика на недельном таймере).

**ПОЛНЫЙ ПЕРЕСБОР С НУЛЯ (2026-05-31, по запросу клиента)**: `scripts/full_rematch.py` (peer-auth эквивалент `rematch --reset` + revalidate, т.к. CLI не читает .env под postgres). Сбросил 4147 авто-матчей (28 ручных is_manual СОХРАНЕНЫ), ре-нормализовал 47585 имён, `match_products` с нуля (уважает ВСЕ match_rejections + строгие guard'ы), затем `revalidate_split` (16 split). **Итог: 8261 matched / 3986 cross-site** (было 8665/4210 — recall-набор cross-bucket отпущен в очередь /matcher, как и ожидалось; bucket-based пересбор консервативнее). Verified: strict=0, revalidate=0, клиентский Çaytikanı-kosmetik canonical_id=NULL. Снапшот отката: `/root/pm-pre-full-rematch-0531.sql.gz` (pg_dump, 11M). API рестартнут. Это разовая операция — НЕ запускать в пайплайне (78% churn); ежедневный `pharmacy-monitor-rematch.timer` 05:00 UTC делает только `--revalidate` (split-only).

**Just finished (2026-06-15): aptek снят с Мака → ScraperAPI premium-фикс, затем Decodo AZ residential на сервере (раз в неделю)**. Клиент захотел полностью внешний скрейпинг (без зависимости от включённого Мака). Единственный сайт на Маке — **aptekonline** (нужен реальный азербайджанский IP).
- **Запись 2026-06-02 «все 3 прокси не дают AZ» — НЕВЕРНА в части ScraperAPI.** ScraperAPI с `country_code=az` отдавал 403 не из-за отсутствия AZ-IP, а из-за непереданного флага `premium=true` (residential). Добавлен per-site `SCRAPER_API_PREMIUM_SITES` ([aptekonline.py](src/scrapers/aptekonline.py), commit `70e1eba`). НО: AZ-геотаргетинг у ScraperAPI только на тарифе **Business $269/мес** («Global geotargeting»; Hobby/Startup = US&EU only) — дорого под раз-в-неделю. Free-триал AZ работал, т.к. триал открывает всё (подтверждено офиц. доками ScraperAPI).
- **Решение — Decodo (ex-Smartproxy) residential, Pay-As-You-Go $4/1GB** (~$0.6/мес под раз-в-неделю; 14-day money-back). Мульти-агентный research-свип (21 агент) отсеял маркетинговые ПУСТЫЕ AZ-пулы (LumiProxy виджет «0 IP», NetNut, Litport, Proxy-Seller, Asocks 404) → Decodo + DataImpulse как кандидаты с реальным пулом. Decodo проверен вживую: `az.decodo.com:300NN` → exit IP `185.146.114.41` (AZ) → aptek HTTP 200 + реальные товары. **Нюанс: ~38% отдельных AZ-IP дают транзиентный `522`** (флайки residential-пула).
- **Код ([src/scrapers/aptekonline.py](src/scrapers/aptekonline.py)):** `_decodo_*` хелперы (host `az.decodo.com`, порты `30001-30010` = разные sticky AZ-IP; env `DECODO_USERNAME/PASSWORD/SITES/PORTS/PAGE_ATTEMPTS`); Decodo **первым** в цепочке прокси для aptek; **`_fetch_page` с ретраем по портам** (новый IP на попытку, деф. 5) — лечит 522. **Failure-семантика (architect HIGH → fixed):** транзиент (сеть/5xx/522) → **пропуск страницы и continue** (НЕ обрыв всей категории!), жёсткий блок (403/401/451) или 3 провала подряд → обрыв (`_HARD_BLOCK_STATUSES`, `_MAX_CONSECUTIVE_PAGE_FAILURES=3`); лог `aptekonline_category_incomplete` для наблюдаемости недозалива. +16 тестов (хелперы + 3 интеграционных ретрая). Architect: H1/M1 FIXED.
- **Прод go-live verified:** `run_265 run_ok products=93530` (на уровне исторических ~93k), **page_skipped=0** (ретрай съел все флайки на масштабе ~90 категорий). Креды в `/etc/pharmacy-monitor/env` (DECODO_*). `@aptekonline.service.d/override.conf` добавляет `--no-alerts` (как было на Маке). **Недельный таймер** `@aptekonline.timer.d/override.conf` → `OnCalendar=Mon *-*-* 02:00:00` (Пн 06:00 Baku, после pharmonline), enabled, next Пн 2026-06-22.
- **NB прод-запуск:** `pharmacy-monitor run --site aptekonline` вручную ПОД ROOT падает на `BaseScraper.__aenter__` (Playwright Chromium нет в `/root/.cache`). Запускать только под `pm` (через systemd-сервис) — у pm Chromium установлен. Категории Decodo'ю браузер не нужен (httpx), но `__aenter__` всё равно лаунчит браузер (для `scrape_promos`).
- **Все 3 сайта теперь на сервере:** pharmonline (IPRoyal DDP, Пн/Ср/Пт 01:00), aloe (direct, daily 03:00), aptek (Decodo, Пн 02:00 weekly). **РЕШЕНИЕ КЛИЕНТА (открыто):** Mac launchd `com.pharmacy-monitor.scrape` (pharmonline+aptek) теперь полностью избыточен — отключить или подержать цикл как DR-фоллбэк.

**FOLLOW-UP (2026-06-15, позже — клиент: «все 3 через Decodo»)**: диагностирован корень падения pharmonline — **IPRoyal HTTP 402 (баланс кончился с 12 июня)**, не AZ-баг (на проде каждый прогон `ddp_connect_exhausted ... HTTP 402`). Клиент решил консолидировать ВСЕ 3 сайта на Decodo.
- **Реализовано:** `_decodo_proxy_for` в [base.py](src/scrapers/base.py) (Playwright-цепочка, для aloe; Decodo первым) + `_decodo_proxy_factory` в [pharmonline_ddp.py](src/scrapers/pharmonline_ddp.py) (DDP/WebSocket). `DECODO_SITES=aptekonline,aloe,pharmonline` на проде.
- **M1 (architect HIGH → done): DDP циклит порт Decodo на КАЖДЫЙ reconnect** (свежий sticky AZ-IP — лечит ~38% флайки; reconnect не лупится в мёртвый IP). `_DDPClient.proxy_url` → `proxy_url_factory` (callable|str|None, зеркало ws_url_factory).
- **ГОЧА (live-discovered): библиотека `websockets` НЕ percent-декодирует пароль из proxy-URL** (в отличие от httpx). → для DDP/websockets-пути пароль RAW, для aptek-httpx остаётся `quote()`'нутым. Encoded `85Yo%3D...` → **HTTP 407**; raw `85Yo=...` → connect ok (подтверждено: getFilterParam=205 категорий через Decodo). aloe Playwright-dict: пароль тоже RAW (Playwright сам кодирует).
- **+11 тестов, architect APPROVE.** Validated: DDP-over-Decodo connect + 205 категорий.
- **БЛОКЕРЫ (открыто, клиенту):** (1) **пополнить Decodo** — PAYG-баланс ~0.8ГБ, на полный pharmonline (тяжёлый) не хватит → иначе Wed-прогон 402'ит. (2) Decodo-аккаунт **UNVERIFIED** — тяжёлая нагрузка на неверифицированном риск KYC/suspension → **рекомендация: ID-верификация Decodo**. (Blocked-targets категориями нас НЕ трогают: streaming/mailing/business; e-commerce не в списке, aptek 93k подтвердил.) aloe валидируется на ближайшем 03:00. Mac launchd пока DR.

**Just finished (2026-06-11): UI-кнопка «Запустить scrape» молча не исполняла очередь — launchd убивал watcher-субшелл**. Нажатие кнопки не приводило к скрейпу. Бэкенд исправен (`POST /api/v1/dash/scrape/trigger` → 202, кладёт `scrape_requests.pending`; требует роль **admin/owner** — viewer ловит 403, что само по себе выглядит как «не работает»). Корень — Mac launchd watcher (`infra/local/watch-scrape-queue.sh`): спавнит detached-субшелл со скрейпом и сразу выходит, а в plist'е `com.pharmacy-monitor.watch.plist` **не было `AbandonProcessGroup`** → launchd при выходе скрипта убивал всю process-group (включая субшелл) за <1с, скрейп не стартовал, request вис в `running`. `& + disown` не спасает (снимает только bash-SIGHUP, не launchd-reap; подтверждено `man launchd.plist`). Так молча сдохли запросы #5/#6/#7.
- **Фикс (core, commit 1):** `AbandonProcessGroup=true` в plist (репо + `~/Library/LaunchAgents/` копия) + `launchctl bootout`/`bootstrap`. Проверено вживую: request #8 реально отскрейпил aptekonline+pharmonline с Mac (субшелл выжил, `pharmacy-monitor run` активен, тики defer'ятся `etime=NN:NN`).
- **Hardening (architect HIGH ×2, commit 2, всё в `watch-scrape-queue.sh`):** (1) **self-heal зависшего прогона** — guard читает `ps -o etime` бегущего `pharmacy-monitor run`; если > `SCRAPE_MAX_HOURS` (деф. 6ч) → kill + помечает его request `failed` через `/scrape-complete` (серверного reaper'а НЕТ → иначе orphan `running` блокирует новые клики анти-спамом 409), затем берёт следующий pending. Раньше зависший прогон вечно заклинивал pgrep-guard (тот же класс, что морозил aptek на дни). (2) **туннель** — переиспользуем персистентный db-tunnel (:5433) если жив, свой ssh поднимаем только если порт свободен, ни того ни другого → fail'им request (было: свой ssh всегда дох на `ExitOnForwardFailure` из-за занятого порта, trap целил в мёртвый PID, скрейп вслепую ехал на чужом туннеле без liveness-чека).
- **NB (architect LOW):** UI-скрейп и ночной `run-scrape.sh` (aptek 18:00 Baku) делят `pgrep -f "pharmacy-monitor run"` namespace — зависший один блокирует другой (теперь self-heal'ится на 6ч). Watcher запускает скрипт ПРЯМО из репо-пути → правка `watch-scrape-queue.sh` = деплой; для plist нужна копия в LaunchAgents + reload. Тесты: `bash -n` + unit (etime-парсер boundary 6ч / request-id extraction) + live (guard корректно defer'ит, #8 цел, launchd stderr пуст). Architect: APPROVE-WITH-NITS.

**Just finished (2026-05-31, commodity cross-brand fix — workflow wmol35wiv)**: **Brand-aware `ultra_equal` + чистка ложных коммодити-матчей**. Клиент нашёл «Çaytikanı yağı 100ml» Fitooil/Herba Flora (pharmonline) склеен с «Çaytikanı yağı 100ml (kosmetik)» Mirrolla (aptek) — мой recall-прогон наклеил cross-brand коммодити, т.к. `ultra_equal` (gate recall + UI auto_safe бейдж) сравнивал только спеки из ИМЕНИ, бренд игнорировал. Мульти-агентный workflow (10 агентов, adversarial-проверка каждого фикса) одобрил **РОВНО один код-фикс**: `ultra_equal += and not _has_conflicting_brand(a,b)` (commodity-gated + fuzzy/NULL/manufacturer-safe — same-firm транслитерации и NULL-brand твины сохранены, trade-name препараты не затронуты). **Отклонил** (спас от регрессии): guard по «kosmetik» (pharmonline пишет `(Kosmetika)` как категорию-паддинг → разорвал бы верные; `qida`=БАД → ещё ложные) и blanket-strip корп-суффиксов (утёк бы pharma/kozmetik). **Отложил**: узкий strip юр-форм (OOO/Ltd). Прод-чистка: backfill `backfill_aptek_slug_brand.py --commit` (18 commodity-товаров получили бренд из 151-словаря) → `apply_brand_split.py --commit` разъединил **cl102883 (Alaqanqal yağı Medoil≠Biola)**. 5 тестов + 219 matcher-сьют зелёные. **Остаток (честно)**: NULL-brand коммодити, где бренд не извлекается из slug (клиентский Mirrolla — slug `OBLEPIXA--MASLO--100ml` без бренда), `ultra_equal` не загардить без данных → (а) `recall_candidates.py --commit` теперь СКИПАЕТ unbranded-commodity пары (→ человеческая /matcher очередь, не авто-линк), (б) чинятся кнопкой «Неправильное сравнение?» в /comparison (durable через match_rejection). Brand coverage растёт инкрементально.

**Just finished (2026-05-31, финал recall — UI-очередь)**: **Авто-ранжированные подсказки в /matcher — закрытие хвоста recall через human-in-loop** (коммит `e78a8f3`, на проде/в git). После авто-применения безопасного остатка (см. ниже) остались пары, которые НЕЛЬЗЯ авто-линковать без падения точности <97% (sub-ultra: pack-омиссия/форм-вариант; site-collision: кластер уже содержит сайт = intra-site дубль). Раньше очередь `/matcher` (через `dash_unmatched_pairs`) требовала от оператора руками искать аналог текст-поиском. Новый эндпоинт **`GET /api/v1/dash/matches/{id}/candidate-analogs?site=S`** ранжирует unmatched-продукты сайта S, прошедшие guard'ы против ВСЕХ членов кластера (`_hard_conflict`+`_pairwise_spec_conflict`, не в `match_rejections`), и помечает ultra-эквивалентные флагом **`auto_safe`** (бейдж «точное совпадение» → one-click). Accept зовёт `add-product` (is_manual=True), reject → `match_rejections`. **`matcher.ultra_equal(a,b)`** — единый источник истины (sig-token SET + size-буква + ingredient-коды + pack + dose + form + variant), используется И recall-скриптом `--ultra`, И бейджем UI. 9 новых тестов (7 ultra_equal + 2 эндпоинт), backend 926 зелёных, tsc/eslint чисто. Это дренаж остатка recall без риска для precision — оператор добивает хвост в пару кликов.

**Just finished (2026-05-31, ещё позже — recall)**: **Ultra-safe авто-сабсет пропущенных cross-site твинов** (коммит `6a72a86`, на проде/в git). Точность уже была подтверждена ≥97% (98.6%, Wilson LB 97.1%), но replay показал РЕАЛЬНЫЙ recall-gap: матчер precision-first BY DESIGN, bucketing (brand,dosage,pack) пропускает твины с разным написанием/опущением. `scripts/recall_candidates.py` блокирует unmatched по первому значимому токену, ищет cross-site пары `token_set_ratio≥fuzz`, прошедшие `_hard_conflict`+`_pairwise_spec_conflict` и не в `match_rejections`. Флаги `--strict` (равные pack/dose/variant/form) и **`--ultra`** (поверх strict: равный НАБОР значимых токенов не подмножество + равные size-буквы M≠L + **равные ingredient-codes** — отсекает D3-vs-D3+K2-опущение, которое жаловался клиент). На проде `--ultra --fuzz 90` дал 586 кандидатов (eyeball 80 шт. → ~98-99% genuine), `--commit` greedy-слинковал **225 новых пар** (`is_manual=False`): 8291→8741 matched, 3985→4210 cross-site, 24/24 genuine. Затем флаг **`--attach`** (рост существующих кластеров: unmatched-продукт присоединяется к кластеру, если joiner проходит `_hard_conflict`+`_pairwise_spec_conflict` против ВСЕХ членов И его сайт свободен — snowball-guard) докинул **+25 three-way завершений** (обычно недостающий aloe к паре pharm+aptek): **8766 matched, 4210 cross-site, 0 same-site дублей глобально**, 25/25 genuine. Доп. live-фикс: `izotonik/hipertonik` в `_VARIANT_WORDS` (Marimer тоничность — реальный split-линейки). Snapshots отката: `products_canon_bak_0531b` (до 225), `products_canon_bak_0531c` (до attach). **Безопасный остаток recall на fuzz≥90 закрыт полностью**; остаётся только шумный хвост fuzz<90 (~10-15% false на eyeball) → будущая UI-очередь suggested-matches (human-in-loop), НЕ авто.

**Just finished (2026-05-31, workflow-аудит + dose/volume)**: **Мульти-агентный замер точности + P0-фиксы** (коммиты `56351e4`,`31bae32`→`e3882f5`, на проде/в git). Клиент попросил workflow → 12 агентов разметили 250 случайных cross-site кластеров + adversarial-verify → **измеренная точность 99.2%** (Wilson 95% LB **97.1%** — цель ≥97% выполнена; dose-расхождений 0/250 = не системно). Скрипт: `scripts/audit_matches.py` + `scripts/matcher-quality-audit` workflow.
- **P0 — pack-volume (баг ИЗВЛЕЧЕНИЯ)**: `extract_pack_size` брал ПЕРВЫЙ `\d+ml` = «5ml» из концентрации «200mq/5ml», не объём флакона → «Azoksin 15ml» и «30ml» в одном bucket. Фикс: `_CONCENTRATION_RE` снимает «X/Yml» до `_PACK_VOLUME_RE`; `extract_total_volume`; guard `_has_conflicting_pack_volume` (блок при разном объёме В ОДНОЙ единице; kg-вес подгузников и ml-vs-g исключены — были false на dry-run).
- **Dose-guard `_has_conflicting_dose`** (NAME-only!): сравнивает НАБОР доз mg/mq/mkg/mcg из имён (много-компонентные Tripliksam 5/1.25/10 vs 5/1.25/5 ловятся). **URL читать НЕЛЬЗЯ**: slug ломает десятичные («7.5mg»→«7-5mg»→ложн.5mg) → ~40 ложных разрывов на dry-run (откатил из 31bae32). Spaced-thousands collapse («1 000 mq»→1000).
- Применено: 22 guard-кластера (volume + dose) + 2 Risek-кластера вручную (aptek-имя без mg, но slug risek-20mg/40mg → cross-matched 40↔20; rejection'ы дают пересобрать 40↔40 на след. скрейпе). Все guard'ы в `_hard_conflict`+`_pairwise_spec_conflict` → авто-revalidate чистит каждый скрейп.
- **Остаток**: aptek-имя-без-mg (Risek-класс) — редкий, не авто-гардится name-only dose'ом (нужен чистый dose-from-slug capture в dosage-поле, но slug-десятичные ненадёжны → отложено). Косметика-вариант (Mustela face vs baby, P1 из аудита) — open-vocab сегменты, для UI-флага.

**Just finished (2026-05-31, ещё позже)**: **Auto-revalidate в пайплайне — корень whack-a-mole** (коммит `563876b`, на проде/в git). После того как клиент в 3-й раз нашёл кривой матч (D3 vs D3+K2 пересоздался как cl102882 после ручного split'а), нашёл НАСТОЯЩИЙ корень: `match_products` линкует широко (bucket+fuzzy), guard'ы в `_hard_conflict` драйвят только ambiguity pre-pass (НЕ блокируют primary-линк), а `find_conflicting_clusters` (detection) — ручная CLI, в пайплайне НЕ вызывалась (`main.py` звал `match_products` и ничего после). → guard-несоответствия (бренд/состав/вариант/сила) пересоздавались КАЖДЫЙ скрейп и висели до ручного `rematch --revalidate`.
- **`matcher.revalidate_split(session, dry_run)`** — бьёт cross-site spec-конфликтные кластеры на spec-когерентные группы (keep большую cross-site, eject выкидышей +reject; dissolve если когерентной нет). Один источник логики для пайплайна + CLI `rematch --revalidate` + `scripts/apply_brand_split.py` (раньше был тупой dissolve-all).
- **Вшит в пайплайн** (`main.py` сразу после `match_products`) → каждый прогон (прод aloe 03:00 + Mac pharmonline/aptek 18:00) авто-чистит guard-классы. **Корень повторяющегося бага закрыт.**
- **`scripts/audit_matches.py`** — проактивный аудит (разброс цен ≥1.8× + расхождение имени). Нашёл НОВЫЙ класс — **вариант-слова**.
- **Вариант-word guard (коммит `eebbcb1`)**: `matcher._has_conflicting_variant_words` — WHITELIST `{plyus,plus,forte,fort,bronxo,bronho,pain}`; разный набор → разные товары (Linkas vs Linkas Plyus, Kodelak vs Kodelak Bronxo, Snip vs Snip pain). Whitelist КРОШЕЧНЫЙ намеренно: dry-run широкого списка дал ~15 ЛОЖНЫХ (размер подгузника = номер: «Sleepy Natural 3» 4-9kq == «Sleepy Natural Midi» 4-9kq; Nivea Men == kişilər перевод; ultra/comfort/active не различают). Эти классы НЕ авто-гардятся (нужна weight-range/translation-логика) → оставлены для UI-флага. 3 кластера разнесены, 0 false. Вшит в _hard_conflict + _pairwise_spec_conflict → авто-чистится. +тесты (вариант + size/translation/form must-not-fire).

**Just finished (2026-05-31, позже)**: **Composition-variant guard — D3 vs D3+K2** (коммит `814cd03`, на проде/в git). Клиент нашёл: «Venatura Vitamin D3» (моно, 13.20) ошибочно склеен с «Venatura Vitamin D3, K2» (комбо D3+K2, 25.60) — тот же бренд (Venatura/Vefa İlaç), объём 20ml, префикс имени, но РАЗНЫЙ состав.
- Корень: matcher bucket'ит по (brand,dosage,pack) + fuzzy имени; «venatura vitamin d3» — почти-префикс «...d3 k2» → высокий fuzzy → матч. Отличающий «K2» (добавленный витамин) не учитывался.
- Фикс: `matcher._has_conflicting_ingredient_codes(a,b)` — извлекает витаминные коды `[dkb]\d{1,2}` (d3,k2,b6,b12…) из имён; конфликт ТОЛЬКО когда у ОБОИХ есть код И наборы различаются (D3 vs D3+K2). Если у одной стороны кода НЕТ — это опущение в названии (DetriBus = DetriBus D3), НЕ конфликт. Wired в `_hard_conflict` + `_pairwise_spec_conflict`.
- Масштаб на проде: ровно **2 кластера** этого класса (Venatura, Kombivit) — оба разнесены; vs ~13 omission/spacing/translit случаев которые правило корректно НЕ трогает.
- `scripts/apply_brand_split.py` обобщён: группирует по полному `_pairwise_spec_conflict` (бренд + ingredient + variant/strength/…), а не только бренд → когерентно разносит ЛЮБОЙ конфликтный кластер. Применено: cl99399 + cl99951 dissolved, 0 flagged. Прод-matcher с guard'ом.

**Just finished (2026-05-31)**: **Brand-aware matcher guard — кросс-брендовые матчи коммодити** (коммиты `efcabec`+`200da10`, на проде/в git). Клиент нашёл: «Alaqanqal yağı 100 ml» Biola (pharmonline 14.00) ошибочно сматчен с Herba Flora (aptek 6.70) — разные фирмы.
- **Корень**: поле `products.brand` — мусор. `brand_catalog.extract_brand()` при промахе каталога фолбэчит на ПЕРВОЕ СЛОВО названия → у 92% pharmonline / 81% aptek `brand` = generic-имя («Alaqanqal»), а не фирма. Matcher не мог различить Biola от Herba Flora.
- **Новая колонка `products.brand_verified`** (migration `0010`, прод на ней + застампан 0009): настоящий бренд из АВТОРИТЕТНОГО источника — pharmonline из URL-slug, aptek из `"brand":{}` JSON на странице товара (category-API бренд НЕ отдаёт; `olke`=страна, `terkib`=состав), aloe из готового поля `brand`.
- **`src/brand_resolver.py`** + guard `_has_conflicting_brand` в matcher (`_hard_conflict` + `_pairwise_spec_conflict`). Блок ТОЛЬКО когда: (1) оба имени — ботанический коммодити (`is_commodity_name`: yağı/toxum/çay/ekstrakt/qabıq…), (2) оба бренда уверенные ПОТРЕБИТЕЛЬСКИЕ, (3) различаются И fuzzy-несхожи (≥72 = одна фирма с разным написанием).
- **Почему так узко (КРИТИЧНО)**: blanket-вариант на dry-run разорвал бы **121 кластер, ~115 ВЕРНЫХ** — aloe и pharmonline пишут один завод по-разному (Biofarm/Biofarm Spzoo, Merk/Merck Sante, Berinqer/Boehringer, Nijfarm/Nizhfarm). Guard заводы-компании (суффиксы GmbH/İlaç/Pharmaceuticals + known-mfr set) считает неразличающими + fuzzy-фильтр гасит транслитерации. Итог dry-run: **121→2** реальных (cl100014 Biola≠Herba Flora = кейс клиента; cl101216 кора дуба Xerbes≠Herba Flora).
- **Применено на прод** через `scripts/apply_brand_split.py --commit` (бренд-когерентный split, НЕ тупой dissolve-all CLI): cl100014 расклеен (оба unmatched), cl101216 — пара Herba Flora сохранена, Xerbes выкинут. 3 rejection'а с reason `brand-guard`. Ongoing: `persist_results` пишет `brand_verified` (pharmonline slug + aloe) на каждом скрейпе; Mac-прогоны (pharmonline+aptek) уже с guard'ом.
- **GAP ЗАКРЫТ (2026-05-31, follow-up, коммит `a6863a3`)**: aptek slug-form коммодити восстановлены через **vocab-match** — `brand_resolver.match_brand_in_text(text, vocab)` матчит slug против словаря потребительских брендов, уже встреченных в `brand_verified` (freq≥3 → редкий мусор типа kosmetik/ternofarm не проходит). `scripts/backfill_aptek_slug_brand.py` проставил бренд 19 aptek-коммодити. Плюс: **алиас Fitooil≡Herba Flora** в `consumer_brand` (Fitooil — масляная линейка Herba Flora; снял 3 ложных разрыва) и фикс slug-парсера pharmonline (strip `?lng=en` query-string + страны azerbayca/belarusiya — перепарсил 20 битых `brand_verified` вроде «Flora Azerbayca»→«Herba Flora»). Итог: **+4 реальных коммодити-разрыва** на проде (масла Arqan/Qreypfrut/Limon/Zeytun — Biola vs Medoil/Fitooil), 0 flagged остаётся. Остаточно: ~45 aptek-коммодити с брендом не в словаре (Talya/Kavvamli/Beyazflora) — безопасно (NULL → не гардятся), подхватятся когда их бренд появится в словаре.
- **TODO деплой на прод**: aloe скрейпится на проде (03:00 UTC) — там СТАРЫЙ код, нужен rsync+restart чтобы aloe-persist писал `brand_verified` и aloe-matching имел guard. Кейс клиента (aptek↔pharmonline) уже защищён Mac-прогонами. 46 brand-тестов (`tests/test_brand_resolver.py`, `tests/test_matcher_brand_guard.py`), сьют зелёный (896, 7 sandbox-only).

**Just finished (2026-05-30, ещё позже)**: **Validation-job для «фантомных» aptekonline-товаров (страница 404)** (`src/link_validator.py`, на проде; коммит TODO):
- Пользователь нашёл матчи где ссылка на aptekonline ведёт на 404. **Диагностика**: aptekonline JSON API (`productList`) листит товары, которых нет как страниц — они скрейпятся (**age=0!**), matched, но `/product/{url_id}` отдаёт 404. ~3-5% сматченных. Исключено: делистинг/staleness (мёртвые age=0 — фильтр по `last_seen` не поможет), баг URL (98% живые), формат `url_id` (мёртвые размазаны ~4% по форматам, чистого разделителя нет). **Единственный сигнал — реальный HTTP-чек.**
- **`Product.url_dead_at`** (timestamp, nullable). **ИСПРАВЛЕНО аудитом 2026-05-30:** прежняя запись «рабочего alembic НЕТ» — ЛОЖЬ. Alembic РАБОТАЕТ (`alembic.ini: script_location = migrations`, `migrations/versions/` 0001–0009). Колонка изначально добавлена ручным `ALTER TABLE` (debt), теперь оформлена как `migrations/versions/0009_product_url_dead_at.py`; на проде `alembic stamp 0009`. Новые колонки — ТОЛЬКО через alembic, не через SQLite-shim в storage.py.
- **`src/link_validator.py`**: `classify` (404/410/451→dead; 2xx/3xx→alive; 403/429/5xx/сеть→**error НЕ трогаем** — транзиент не должен скрыть живой/воскресить мёртвый), async `check_urls` (httpx, browser UA, `trust_env=False`=direct, GET для надёжного hard-404), `apply_results`. 6 тестов (httpx MockTransport).
- **CLI `validate-links --site aptekonline [--limit --concurrency --matched-only]`** ([src/main.py](src/main.py)).
- **Фильтр**: `dash_comparison` ([src/api.py](src/api.py)) + `category_comparison`/`_iter_matched_prices` ([src/analytics.py](src/analytics.py)) исключают `url_dead_at` товары (мёртвый клиент → матч выпадает; мёртвый конкурент → скрыт). 4 теста.
- **Расписание**: в Mac launchd [run-scrape.sh](infra/local/run-scrape.sh) после aptekonline-скрейпа (**Baku-IP direct, без прокси** — Hetzner забанен; residential-прокси на 3894 product-страницы сжёг бы трафик). Non-fatal.
- Деплой: ALTER + rsync кода + API restart (verified: CLI registered, category_comparison=170). **Первый validate-links оставлен ночному Mac launchd** (по выбору юзера). 828 тестов, e2e vs реальный aptekonline ✓ ({dead, alive, alive}).

**Just finished (2026-05-30, позже)**: **Matcher вариант-guard (серебро Ag + номер модели type/tip/тип N) — хирургия вместо rematch** (`src/matcher.py`, на проде; коммит TODO):
- Пользователь нашёл ложный матч `Spiral Yunona Bio-T Ag` (серебро) ↔ `Bio-T Tip 1` (тип) — целый класс в линейках с модификациями (Юнона Био-Т: Ag/Cu380/type1/2/Super). Корень: «Ag» (2 буквы) < `_MIN_VARIANT_TOKEN_LEN=3` → игнорился; type/tip N не сверялся.
- **`_has_conflicting_variant_marker`** ([src/matcher.py](src/matcher.py)): серебро→маркер `ag`, type/tip/тип N→`t<N>`; СИММЕТРИЧНОЕ правило (как variant_tokens/atoms) — блок при взаимно-уникальных маркерах. **Ловушка** az `ağ`=белый / `AG`=антиген обойдена: detection БЕЗ strip_accents (ğ≠g) + симметрия (односторонний `{ag}` vs `{}` НЕ блокирует, т.е. «Maska (ag)» vs «Maska» ок). type-номер 1-2 цифры+`\b` → «Cu380 type1»→{t1} (380 не зацепляется). Встроен в `_hard_conflict` + 4 прохода. 8 unit-тестов, 130 matcher-тестов.
- **Прогон + churn dry-run** (важная методология): полный matcher на 47582 товарах — guard трогает только **−2** (без переблокировки). НО полный `--reset` rematch дал бы **78% churn** (текущие кластеры строились инкрементально с reuse canonical_id; from-scratch расходится по greedy-порядку) → **ОТКАЗАЛИСЬ от rematch**. Surgical-скан текущих кластеров: ровно **2** с конфликтом.
- **Деплой (хирургия):** matcher.py на прод + API restart; reject ровно 2 кластера (`100259` Ag↔Tip1 = кейс юзера + `100261` Ag↔type1) через `match_actions.add_rejection` + canonical_id=None + delete Match (зеркало `/matches/{id}/reject`). Near-zero churn; guard+rejection не дадут вернуться; `Ag↔Ag` сматчится на след. скрейпе. **Урок: для точечных matcher-фиксов — surgical reject флагнутых кластеров, НЕ full `--reset` rematch (78% churn).**
- **Обобщение (generalize):** добавлены `super`,`multi` в `_PHARMA_MODIFIERS` (Bio-T Super/Multi axis; asymmetric modifier-guard). Валидация: replay 8309 = идентично (zero over-block, чистое future-proofing). **Catalog-wide sweep** полным guard-set (4006 cross-site кластеров): только 5 флагнуто — 2 Bio-T (variant_marker, уже rejected) + 3 false-positive (Huggies `variant_tokens` descriptive-text, Lordes `form` məhlul/şərbət = одна жидкая форма). Вывод: вариант-проблема была по сути только в линейке Био-Т, остальной каталог чист. Принцип матчера: курируемые дискриминаторы (silver/type-N/modifier/dim/%/country) + симметричное правило, НЕ blanket-эвристика «любой лишний токен = другой» (она бы переблокировала verbose-vs-terse — те самые Huggies/Lordes).

**Just finished (2026-05-30)**: **Сравнение категорий (новая фича) + фикс сломанного price-index + чистка ru-ярлыков категорий** (коммит `774a723`, всё на проде/в git):
- **Бэкенд** ([src/analytics.py](src/analytics.py), [src/api.py](src/api.py)): `price_index_by_category` **была сломана в рантайме** — `api.py:1940` звал с `client_site=`, а сигнатура брала только `session` → **TypeError на каждом `/price-index`** (секция в Аналитике 500'ила; тесты не ловили — звали без kwarg). Фикс: `*, client_site/tenant_id/min_confidence`; + переход с snapshot'ов последнего run_id на `latest_snapshots_per_product` (diff-only-safe). Новая `category_comparison()` + `_iter_matched_prices` helper + dataclass `CategoryComparison`: per-категория средняя клиента, **per-site средние конкурентов отдельно** (aptekonline/aloe), ценовой индекс (100=паритет), cheaper/pricier/parity counts; tenant + confidence-floor 0.70; ярлык через join к `Category` (label_ru/az) с slug-fallback. Эндпоинт `GET /api/v1/dash/category-comparison` (locale-ярлык) + опц. `category=` drill-down на `/comparison`.
- **Фронт**: новая страница `/category-comparison` (сортируемая таблица + KPI, расцветка индекса <100 зелёный/>100 красный, клик по строке → `/comparison?category=`, mobile-cards, CSV). `comparison` читает `?category` (чип-фильтр), Аналитика price-index починена + строки кликабельны, nav-ссылка «Сравнение категорий» (НЕ путать с «Категории» в Настройках = `/categories` управление), i18n ru/az/en. **Гоча**: `next.config.mjs` `LEGACY_ROUTES` — при `localePrefix:"always"` URL без локали 404'ит, пока роут не добавлен в редирект-список (поймали на проде, добавил).
- **Чистка ярлыков** ([data/category_labels_ru.json](data/category_labels_ru.json)): 197 из 205 pharmonline-категорий имели **азербайджанский текст в `label_ru`** (label_ru==label_az). Перевёл az→ru (162 уникальных), применил к прод-БД (UPDATE 197 строк, бэкап `data/category_labels_ru_backup_20260530-*.json` на проде для отката). Проверено: 0 осталось не-кириллицей. **NOT done**: 305 категорий aptekonline/aloe (без pharmonline_slug) не трогал — не на странице сравнения, но на `/categories` могут быть на az.
- **Тесты**: +21 (category_comparison мульти-кат, **diff-only регрессия**, tenant-изоляция, label-fallback, confidence-floor; `/category-comparison` auth+tenant; `/comparison?category`). 810 проходят, ruff/tsc/eslint/next build чисто. Деплой: tar-over-ssh changed files → pnpm build как pm → restart api+frontend как root. Прод-проверка: 170 категорий с ru-ярлыками, индекс/per-site работают.

**Just finished (2026-05-29)**: **Comparison-фича закалена end-to-end + matcher precision/recall + дедуп + ручной relink + pharmonline coverage fix + DDP robustness** (13 деплоев, всё на проде/в git):
- **pharmonline DDP robustness (Part 1)** ([src/scrapers/pharmonline_ddp.py](src/scrapers/pharmonline_ddp.py), коммит `bd9ddb3`): DDP-клиент интермиттентно крэшил (`timed out during opening handshake`, exit 1) и висел 5+ мин (`ws.send` на half-open сокете без таймаута). Фиксы: `_connect`→retry+exp-backoff обёртка (`_connect_once` с `open_timeout` + `asyncio.wait_for` на post-connect SockJS/DDP handshake `_ddp_handshake`); `_send_and_wait`→весь метод (вкл. send) в `asyncio.wait_for(timeout)`; `call()`→ловит TimeoutError, цикл `_CALL_ATTEMPTS` reconnect+backoff. Env-конфиг (читается при вызове): `PHARMONLINE_DDP_{OPEN_TIMEOUT=20,CONNECT_ATTEMPTS=4,CONNECT_BACKOFF=2,CALL_ATTEMPTS=3,CALL_RETRY_BACKOFF=1}`. 7 тестов (fake ws). **Валидировано на проде**: при падении прокси видно `ddp_connect_retry 2/4/8с`→`ddp_call_exhausted`→чистый raise вместо зависания. Эффект: зависшая категория теперь fail-fast→`scrape_category` пропускает→прогон **доходит до конца**→at-end persist сохраняет успешные (раньше hang→0 сохранено).
- **Part 2 (fast empty-query sweep) ОТКЛЮЧЁН** — probe показал: `products` с `{"query":{}}` это **featured-выборка** (~100, offset почти не двигается: offset 0→100, 120→220 uniq), НЕ каталог. Single-pass невозможен. Полное покрытие — только per-category (205 кат). Инкрементальный per-category persist отложен (Part 1 уже чинит hang→0; малая доп. ценность).
- **IPRoyal баланс + расписание (РЕШЕНО 2026-05-29):** probe вскрыл что IPRoyal residential выдавал `HTTP 402` — кончился трафик (план 2 GB/мес, исчерпан сегодняшними прогонами+probe). Пользователь **пополнил (2 GB)** → connect восстановлен (`CONNECT_OK cat_map=205`), end-to-end smoke ок (`run --category-id 1` → `run_ok products=97`). Чтобы 2 GB/мес хватало (полный 205-кат прогон ~100-150 MB; daily не влез бы), **pharmonline переведён на 3×/неделю**: drop-in `/etc/systemd/system/pharmacy-monitor-scrape@pharmonline.timer.d/override.conf` → `OnCalendar=Mon,Wed,Fri *-*-* 01:00:00` (aptek 02:00/aloe 03:00 остались daily — другие прокси, без бюджета). Первый полный прогон — Пн. **Рекомендация пользователю:** включить IPRoyal Auto top-up (порог) — чтобы не уйти в 0 снова. `TimeoutStartSec=36000`(10ч) оставлен. **Будущее (если нужно daily-full pharmonline):** 10 GB план ($52/мес) ИЛИ мульти-провайдер fallback DDP (IPRoyal→BrightData, чтобы один провайдер не был SPOF — отдельная задача).
- **pharmonline coverage fix (53→205 категорий)** ([src/main.py](src/main.py) `category sync-pharmonline`, коммит `e055881`): диагностика — ночной pharmonline-прогон покрывал только ~49% каталога (9694/19631 за 24ч), хотя завершался чисто (`failures=0`, не таймаут). Корень: в БД было заведено **53** категории, а `getFilterParam` (DDP) возвращает **205** — 152 категории (~8000 товаров) не сканировались. Команда `category sync-pharmonline` коннектится по DDP, зовёт getFilterParam → upsert недостающих (key=`pharma_{slug}`). Запущена на проде: 53→**205**. `TimeoutStartSec` поднят 4ч→**10ч** в `pharmacy-monitor-scrape@.service` (205 категорий через медленный DDP+residential ≈ 7-8ч). ⚠️ **Известный follow-up:** pharmonline DDP throughput ~99 fetch/мин (aptek httpx 2011/мин, в 20× быстрее) — reconnects + residential latency. Ускорить можно empty-query single-pass (`products` с `{query:{}}` без category-overlap) — отдельная задача. aptekonline(304)/aloe(5) уже покрываются на 99%.
- **Manual relink by URL** ([src/api.py](src/api.py) `POST /api/v1/dash/matches/{id}/relink`, [frontend comparison/page.tsx](frontend/src/app/[locale]/(dashboard)/comparison/page.tsx) `RelinkPanel`, коммит `556cd40`): в раскрытии строки /comparison на каждый сайт поле «вставить URL правильного товара» + «Применить». Резолвит URL→Product (external_id из последнего сегмента, fallback url ilike, tenant+site), вызывает `match_actions.swap_alternative` (старый товар сайта отвязывается +rejection, новый привязывается, match→is_manual → rematch не тронет). Покрывает замену И добавление недостающего сайта. 404 если URL не в каталоге, 409 если товар уже в другом кластере. 5 backend-тестов. Для случаев, где авто-матчер принципиально не дотянет (same-country разные производители).
- **Freshness в `/comparison`** ([src/api.py](src/api.py) `_comparison_spread` + `_price_age_days`): цена считается свежей по `Product.last_seen_at` (НЕ `captured_at` — под diff-only у стабильной цены он старый). Stale (>14д) показывается с бейджем «N дн. назад», но НЕ участвует в spread/min/max/cheapest. Frontend: `PriceCell` приглушает + зачёркивает stale, i18n `stale_badge`/`stale_note` (ru/az/en).
- **High-outlier guard** ([src/api.py](src/api.py)): симметрично low-outlier — при 3+ свежих сайтах дропает цену >8.3× медианы (wrong-match «Aspirin C» 8.02 при 0.30/0.30). + ручной reject 7 wrong-match (Mustela, Mikrazim-доза, Normoqlip-вариант…) + хирургический unlink Aspirin C из 3-товарного кластера.
- **Matcher variant-atom guard** ([src/matcher.py](src/matcher.py) `_has_conflicting_variant_atoms`): сравнивает «вариант-атомы» из RAW-имени (серийная цифра 1-9 + буква-вариант кроме юнитов q/g/l + одиночная фарм-масса 1-9 mg) — ловит Normoqlip M≠2≠4mg, Lorinden C≠A, Vitamin A≠C, ASferon C≠S, Solgar C≠E, Güzgü M≠S. normalize вырезал эти различители рядом с pack → guard'ы их не видели. Валидировано на дампе 57142 (0 ложных).
- **Matcher ambiguity-suppression + multi-digit strength guard** ([src/matcher.py](src/matcher.py), коммиты `82ce31d`→`c252211`→`3634cda`, Codex-reviewed): (1) **ambiguity pre-pass** — генерик-якорь, fuzzy-матчащийся к ≥2 кросс-сайт кандидатам с РАЗНЫМИ добавочными токенами (Altay/Mirrolla/Seide) → подавляется (`ambiguous_ids`). Финальная логика: `_hard_conflict(q,p)` считает кандидата только если настоящий проход его не отверг бы (Nutrilon Premium 1 не теряет двойника из-за Comfort/Pepti); subset-условие `q⊆p` (взаимно-разные бренды Medoil vs Fitooil НЕ подавляют друг друга). ⚠️ equal-peer gate (`c252211`) дал **регрессию** — плоские генерики с несколькими двойниками спаривались произвольно (инвертированные цены) — заменён на guard-aware+subset в `3634cda`. (2) `_has_conflicting_strength_number` — 4-7-значная enzyme/IU/BV сила (Mikrazim 25000≠10000) во всех 4 проходах. **Codex MCP-ревью**: 1 HIGH + 2 MED пофикшены. Прод: ~290 генериков подавлено, **0 cross-brand на всей БД** (скан 47582 по altay/mirrolla/medoil/seide/…), total ~4320.
- **Cross-form/substance normalize-фикс** ([src/normalize.py](src/normalize.py), коммит `4ae53ec`): клиент пожаловался «товары другие» в топе spread — оказалось cross-FORM (не cross-brand). (1) `_TREE_PLANT_RE` склеивает биграмму «çay/şam ağacı» (чайное дерево / сосна — растения) ДО стрипа: раньше «çay»(category-prefix) и «şam»(форма-суппозиторий) вырезались → оба → «agaci yagi» → ложный матч чай↔сосна. (2) `yağı`/`yağ` → форма «oil» в `_FORMS`+`_FORM_CANONICAL`: «Qliserin yağı»(oil) ≠ «Qliserin Məhlul»(solution). Масло↔масло (Çaytikanı обоих сайтов) = одна форма → матч цел. Ре-валидация (ре-нормализация дампа): oil/solution split, чай↔сосна 0, Afalaza(гомеопатия)/Nestogen/Doksisiklin целы. 776 тестов. **NB: form-synonym `0137b5d` НЕ откатывали** — эмпирически подтверждено что он к cross-form непричастен (только tabletlər/kapsulalar).
- **Country-aware guard** ([src/matcher.py](src/matcher.py) `_has_conflicting_country`, коммит `cded2b6`): клиент показал «Qliserin 50ml» phar 1.30 (Azerfarm, Azərbaycan, мед.) ↔ apte 8.65 (Talya, Türkiyə, косметический) = РАЗНЫЕ производители, ложный 85%. Страна УЖЕ в данных (apte: `Product.manufacturer`=«olke»/страна; phar: хвост URL-слага `…azerfarm-mmc-azerbaycan`/`…nobel-ilac-turkiye`), матчер не использовал. `_COUNTRY_CANON` (AZ/EN→ISO), `_country_of(p)` (поле manufacturer ИЛИ URL-хвост, фильтр по словарю — «ml»/«tabletler» не страна), блок если у обоих страна известна и различается (одна неизвестна→не блок). Прод `rematch`: total 4320→**4007** (−313 cross-country ложных), cross-country кластеров ~0, Qliserin теперь ru↔ru/ua↔ua/az↔az. 783 теста. ⚠️ aptekonline хранит только СТРАНУ (не имя производителя) — same-country разные-производители НЕ ловятся (нужен detail-scrape имени производителя, отдельная задача).
- **Dimension + concentration guards** ([src/matcher.py](src/matcher.py) `_has_conflicting_dimensions` + `_has_conflicting_concentration`, коммит `995e818`): клиент показал «Leykoplastr Alban 10sm×10sm» (apte) ↔ «10sm×25sm» (phar) = разный размер, ложный 63%. Два неучтённых спец-класса: (1) **габариты** AxB — `_dimensions` извлекает «N[ед]×N ед» (mm/sm/cm/m, разделитель x/х/×/*, смешанные единицы «5m×2.5sm», без ед у первого), нормализует в мм, сравнивает неупорядоченно (10×30==30×10); «10×5 ml» НЕ размер (negative-lookahead на ml). (2) **концентрация %** — extract_dosage % не ловил («3%»→15q): теперь Tetrasiklin 3%≠1%, Novokain 2%≠0.5%. Блок только если у обоих атрибут есть и различается. Прод rematch: total 4014→4007 (−7); diff_dimension 0, diff_concentration 0; Leykoplastr 10×10↔10×25 убран, Tetrasiklin 3%↔3%/1%↔1%. 791 тест. **Матчер теперь сверяет: бренд + страна + форма + доза + pack + сила + размер + концентрация + вариант/серия/гендер.**
- **Остаток (известно, НЕ баг матчинга):** топ spread теперь — легит same-country same-product с реальной разницей цен (Tetrasiklin, Diklofenak, Levomekol — ради этого тулза) + pack-size mismatch (Leykoplastr размеры, маски поштучно↔коробка — нужен count-parser) + brand-stub (generic↔Botalife/Braun) на ручной reject. `price=0` (~17, нет в наличии) уже дропается outlier-фильтром `_comparison_spread`.
- **Form-synonym recall fix** ([src/normalize.py](src/normalize.py) `_FORM_CANONICAL`): канонизированы 16 AZ-плюралов/синонимов (tabletlər/sorma/kapsulalar/ampoules…) — раньше «sorma tabletlər» ≠ «tabletlər» ложно блокировало гомеопатию (Afalaza/Anaferon/Divaza). +15 матчей. Группы форм остаются раздельными (cream≠ointment).
- **EN-locale дедуп** ([src/scrapers/pharmonline.py](src/scrapers/pharmonline.py) `_external_id_from_href`): Playwright-скрейпер клал `?lng=en` в external_id → ~9560 EN-дублей. Root-fix (обрезка query) + удалено 9560 дублей с прода (бэкап `pre-endup-dedup-*.sql.gz`).
- **`notify test`** ([src/main.py](src/main.py)): smoke-тест доставки (email+telegram одной командой). + удалён мёртвый `scripts/backup.sh` (SQLite; прод на Postgres `infra/scripts/backup.sh`), provision/RUNBOOK/README реконсилированы.
- **Состояние интеграций (verified 2026-05-29)**: Resend email ✓ работает (daily digest), Sentry ✓ работает, локальный Postgres-бэкап ✓ (GPG). Не настроено: Telegram (опц.), B2 offsite (отклонено клиентом), HA-резерв.
- **Observability ✓ (2026-05-29)**: Prometheus (apt, :9090, скрейпит `/metrics`+self, 7 alert-rules) + Grafana (apt, :3001, дашборд «Pharmacy Monitor» 10 панелей) за Caddy → **https://leaddrive.cloud/grafana** (дефолт admin/admin ОТКЛЮЧЁН — поставь пароль: `grafana cli admin reset-admin-password <new>`). См. docs/RUNBOOK.md «Observability». node_exporter/Alertmanager не подняты (опц.).
- **Mac-зависимость снята (verified)**: прод-таймеры скрейпят все 3 сайта через прокси (pharmonline→IPRoyal DDP, aptekonline→BrightData, aloe→direct). Mac launchd теперь избыточный DR-fallback. (CLAUDE.md runtime-секция была устаревшей — поправлена.)
- **Matcher у потолка**: recall 98.3% (2253/2293 кросс-сайт-идентичных групп сматчены), precision закалён. Дальше по матчеру/дедупу — убывающая отдача (нет barcode: pharmonline ~13%, aptek/aloe 0).
- Коммиты: ce9ecf3→b97a3e1 (freshness+Codex), 1bd15db (high-outlier), 5307ce1→10aa115 (variant-atom), 82ddabf (EN-dedup), 0137b5d (form-fix), 6c3ced2 (notify test), 68832e5 (docs). 764 теста.

**Just finished (2026-05-28 morning)**: **9 MCPs всё user-scope + persistent Postgres tunnel + overnight pipeline green + 4 commits pushed**.

### MCP stack (`~/.claude.json`, user-scope, во всех проектах):

| MCP | Purpose | Cost | Tools | Notes |
|---|---|---|---|---|
| `openrouter-sonar` | Existing Sonar (4 tools) | OpenRouter credits exhausted — нужен top-up | 4 (sonar_ask/reason/research/search) | Backup для cross-check, сейчас 402 |
| `perplexity-ask` | Anti-hallucination primary | $50 prepaid, ~$0.005-0.02/req | 1 (perplexity_ask) | Sonar Pro native API |
| `firecrawl` | Web scraping + extraction | Free 1000/мес (988 left) | ~24 (scrape, crawl, map, search, extract, monitor, agent, interact, browser_*) | **Cloudflare bypass работает для pharmonline** ✓ |
| `memory` | Persistent cross-session knowledge graph | Free local | 9 (create/search/delete entities, relations, observations) | JSON-граф local |
| `postgres` | Direct DB queries без SSH | Free local | 19 (pg_execute_query/sql/mutation, pg_manage_*, pg_monitor) | Через persistent SSH tunnel `:5433` |
| `github` | PRs/issues/code search natively | Free | ~40+ (create_pr, search_code, get_file_contents, push_files, list_*, manage reviews) | PAT, repo `rashadrahimov/pharmacy-monitor` |
| `brave-search` | Independent search engine для cross-check | Free 2000/мес | 6 (web/news/image/video/local/llm_context_search) | Заменяет dead openrouter-sonar |
| `playwright` | Cross-browser automation (E2E) | Free local (300MB Chromium) | 23 (browser_navigate/click/fill/snapshot/screenshot/evaluate, тaby/dialog/network) | Microsoft official |
| `chrome-devtools` | Debug live Chrome (Network/Console/Perf) | Free local | 30+ (navigate, click, list_network_requests, performance_*, lighthouse, take_heapsnapshot) | Google Chrome team official |

### Persistent SSH tunnel for Postgres MCP

- launchd plist: `~/Library/LaunchAgents/com.pharmacy-monitor.db-tunnel.plist`
- Tracked в проекте: `infra/local/com.pharmacy-monitor.db-tunnel.plist`
- Logs: `~/Library/Logs/pharmacy-monitor-db-tunnel.log`
- Auto-start при логине, auto-restart если SSH упадёт, ThrottleInterval=30s
- Management: `launchctl bootout|bootstrap gui/$UID/com.pharmacy-monitor.db-tunnel`

### Overnight pipeline verified 2026-05-28 (все 5 runs green):

| Run | UTC | Site | Dur | Products | Type |
|---|---|---|---|---|---|
| 110 | 01:02 | pharmonline | 28m | 261,519 | nightly timer |
| 111 | 02:04 | aptekonline | 65m | 89,375 | Mac launchd |
| 112 | 03:04 | aloe | 27m | 1,874 | nightly timer |
| 113 | 05:00 | pharmonline | 7m | 10,000 | **Phase 5.1 intraday ✓** |
| 114 | 06:00 | aloe | 5m | 64 | **Phase 5.1 intraday ✓** |

Backup 04:02 UTC ✓ (GPG encrypted, 10MB). /health up, db 0.97ms, redis 3.03ms, all sites < 4h fresh.

### Exposed API keys (rotate when convenient):
- Perplexity: `pplx-9JgqRvCJ...` — perplexity.ai/settings/api → Regenerate
- Firecrawl: `fc-d0f71c2c...` — firecrawl.dev/dashboard → API Keys → Regenerate
- Brave: `BSAMo35R...` — api-dashboard.search.brave.com/app/keys → Revoke + new
- GitHub PAT: `github_pat_11BZ7WQWY00X...` — github.com/settings/personal-access-tokens → revoke

After rotation, re-register via `claude mcp remove <name> -s user && claude mcp add ...`.

---

**Just installed (2026-05-28 0:00)**: **Perplexity MCP + Firecrawl MCP user-scope** для cross-check anti-hallucination и web scraping fallback.

**Perplexity MCP** (`perplexity-ask`):
- Config: `/Users/rashadrahimov/.claude.json` → `npx -y server-perplexity-ask`
- Tool `mcp__perplexity-ask__perplexity_ask` (после рестарта Claude Code)
- Дополняет существующий `mcp__openrouter-sonar__*` (4 tool'а через OpenRouter, но **credits exhausted — top up если нужен dual cross-check**). 
- $50 кредитов на Perplexity API. Стоимость ~$0.005-0.02/запрос.
- **First win**: за 2 запроса ($0.04) распарсили root cause Phase 6.1 i18n блокера — это [next-intl issue #524](https://github.com/amannn/next-intl/issues/524). См. task #31.

**Firecrawl MCP** (`firecrawl`):
- Config: `/Users/rashadrahimov/.claude.json` → `npx -y firecrawl-mcp`
- Free tier 1000 credits/мес (~1 credit per scrape, ~2 per extract). Billing 2026-05-28 → 2026-06-28.
- Tools после рестарта: `mcp__firecrawl__firecrawl_scrape/batch_scrape/crawl/map/search/extract/agent/interact`.
- **CRITICAL FINDING — Cloudflare bypass для pharmonline.az работает**. Real test: `korinfar-10-mg-100-heb-pliva-almaniya?lng=en` → status 200, full Markdown, LLM-prompt extracted `{name, priceAZN, manufacturer, barcode}` корректно. Это потенциальная альтернатива DDP/IPRoyal для конкретных use cases.
- **Применимо у нас для**:
  - AI crawler fallback (замена Anthropic API extraction — Firecrawl дешевле для этого)
  - Barcode backfill (25k+ pharmonline products без barcode)
  - Quality-control re-scrape подозрительных match'ей
- **НЕ применимо**: full daily scrape (25k+9k+1.8k products × 30 days = 1M+ credits, не влезет даже в Growth план).

**Attempted (2026-05-27 night, ROLLED BACK)**: **Phase 6.1 URL-based i18n** — full migration to `app/[locale]/(dashboard)/...` structure, next-intl middleware с `localePrefix: 'as-needed'`, locale-aware Router + Link через `createNavigation`.

- **Code passed local + prod build** (42 routes, middleware 39.2 KB)
- **Production rendered HTTP 500**: middleware emits `x-middleware-rewrite: http://localhost:3000/<path>` header → Next.js standalone тратит этот URL как HTTP proxy fetch к самому себе → infinite recursion / ECONNRESET. Root cause: `output: 'standalone'` build обрабатывает middleware rewrite через actual HTTP request к localhost:3000 вместо internal dispatch.
- **Attempted workarounds**: `alternateLinks: false` (не помог), `NODE_OPTIONS=--dns-result-order=ipv4first` (изменил ECONNREFUSED→ECONNRESET, recursion остался).
- **Rolled back**: восстановлен tar backup `frontend-src-pre-i18n-20260527-195718.tgz`, cookie-based i18n работает. Локальные изменения в `git stash`: `Phase 6.1 i18n attempt — recursive proxy in standalone build`. Branch `wip/phase6.1-i18n-attempt` создан.
- **Для retry**: попробовать (1) `localePrefix: 'always'` (не уверен поможет, та же middleware mechanic), (2) убрать `output: 'standalone'` и развернуть полный `.next/`, (3) подождать апстрим-фикса в Next.js / next-intl для standalone + middleware rewrite.

**Just finished (2026-05-27 evening)**: **URL fix + Phase 4.5 MAP + Phase 5.5 X-Request-ID + Phase 5.1 intraday** (commits `4765620`, `7e5a541`, `1090db5`, `a95f1ca`):

- **URL update bug в `persist_results` (commit `4765620`)**: `existing.url` НИКОГДА не обновлялся → 6238 pharmonline-продуктов застряли на `https://pharmonline.az/product/True` (артефакт старого `postQuery` бага в DDP). Plus: `ScrapedProduct` dataclass даже не имел поля `barcode` (task #12 был неполон). Фикс: `existing.url = sp.url or existing.url` + `existing.barcode` conditional fill + добавлен `barcode` в `ScrapedProduct`. **Verified on prod: 6238 → 2** (99.97% fix rate, оставшиеся 2 — orphan products not re-scraped). +4 регрессионных теста в [tests/test_persist_results.py](tests/test_persist_results.py). Run 108 показал: 272k products в 51 минуту, persist завершил все URL'ы корректно.
- **Phase 4.5 — MAP violation detection (commit `7e5a541`)**: новый ROI action type `map_violation` — трекает min цены конкурентов per brand за 30д. Если клиент дешевле этого floor более чем на 5% — флагуем opportunity. Severity buckets: critical (≥20%), warning (≥10%), info (≥5%). Min 3 snapshot-семпла per бренд для валидного floor. Отличается от `_price_raise_opportunities` тем, что агрегат по БРЕНДУ (не один матч), 30-дневное окно (стабильнее). +5 регрессионных тестов в [tests/test_roi.py](tests/test_roi.py). Bonus: фикс pre-existing NameError в [src/analytics.py:75](src/analytics.py) (`by_brand[brand][site]` → `[site_name]`).
- **Phase 5.5 — Frontend X-Request-ID propagation (commit `1090db5`)**: каждый fetch генерит UUID v4 (crypto.randomUUID, Math.random fallback), отправляет как `X-Request-ID` header, backend echoes back, ApiError несёт `requestId` для support correlation. Новая helper `getRequestId(err)` для consumers. Round-trip verified: `curl -H "X-Request-ID: test" /health` возвращает тот же header.
- **Orphan runs cleanup**: run 97 (вчера Mac, 102k products, orphan "running") → marked `ok`; run 99 (утренний empty test) → marked `failed`. Дашборд status'ы теперь чистые.

Production state: run 108 OK (272018 products, 11 alerts, GPG-encrypted backup ~32MB). 25 726 pharmonline products с правильными URLs. /health: db_ping 0.62ms, redis_ping 2.62ms, all sites < 24h fresh.

**Just finished (2026-05-27)**: **Phase 2 — matcher v2 with barcode** (commit `97b7526`):

- **`products.barcode` column** (varchar(40), indexed) — Alembic migration `0007_product_barcode` применена на проде. Унифицирует EAN/GTIN/UPC в один canonical field. `ix_products_barcode` btree index для O(log n) lookup.
- **Barcode extraction** в 2 из 3 scraper'ов:
  - **aloe.py** — JSON-LD `gtin13`/`gtin14`/`gtin12`/`gtin8`/`gtin` priority order через shared helper `_extract_barcode_from_jsonld` в [src/scrapers/ai_crawler.py](src/scrapers/ai_crawler.py)
  - **pharmonline.py** — JSON-LD первым, HTML table regex fallback (`Ştrix kod`, `Штрих-код`, `Barcode`, `EAN`, `GTIN` patterns)
  - **aptekonline.py** — **SKIPPED**: listing JSON не содержит barcode, detail page удвоит Bright Data расходы. Когда нужно — отдельная batch-harvest job.
- **Matcher v2 priority-0 pass** ([src/matcher.py](src/matcher.py)): группирует продукты по barcode → cluster cross-site → confidence=1.0. Skip name heuristics (modifier conflicts, series numbers — barcode trumps name). Respect `MatchRejection` + per-unit price sanity. Logged: `matcher_barcode_pass unique_barcodes=N clusters_created=M`.
- **`scripts/rematch_with_barcode.py`** — bulk-break low-confidence (< 0.85, not `is_manual`) clusters когда their members disagree on barcode. Idempotent, dry-run by default, `--apply` коммитит и re-runs matcher для подбора новых pairs.
- **persist_results** ([src/main.py](src/main.py)) — копирует `sp.barcode` в `Product`, never overwrites existing non-null value (защита от scrape runs которые временно не имеют field'а).
- **26 unit tests**: 11 для barcode extraction, 7 для matcher barcode pass, 8 для rematch decision logic. Full suite: **386 pass**, 9 pre-existing failures без изменений.

**Что отложено**: Phase 2.5 (UI suggestion queue для unmatched borderline cases) — отдельная frontend сессия.

**Just finished (2026-05-27)**: **Phase 1 — scraper resilience**:

- **Bright Data Web Unlocker** ($1.50/CPM) — zone `pharmacy_unlocker`, native proxy mode (`brd.superproxy.io:33335`). Allowed IP whitelist: 46.225.149.52 (prod). Phase 1.2 код в [src/scrapers/base.py](src/scrapers/base.py) (`_brightdata_proxy_for`) + [src/scrapers/aptekonline.py](src/scrapers/aptekonline.py) (`_brightdata_httpx_proxy_for`) подхватывает env `BRIGHTDATA_USERNAME/PASSWORD/SITES/HOST`.
- **aptekonline переехал на прод**: live test — 2264+431+111 продуктов из 3 категорий, все HTTP 200, ~30 сек. Лог: `aptekonline_using_brightdata`. Таймер `pharmacy-monitor-scrape@aptekonline.timer` enabled+started.
- **pharmonline остался на Mac**: Web Unlocker даёт HTTP 502 для pharmonline.az независимо от country (az/tr/ru tested). Прямой curl с Baku-IP даёт 200 → значит pharmonline блочит residential pool Bright Data. Open question: escalate в BD support или попробовать Smartproxy/Oxylabs. Pragmatic решение — Mac launchd для pharmonline остаётся.
- **AI crawler fallback** ([src/main.py](src/main.py)) — wired как опт-ин через `AI_FALLBACK_ENABLED=1` env. Срабатывает когда primary scraper отдаёт < 50% от baseline. Disabled by default — burns Claude tokens.
- **ScraperAPI default pool** (`SCRAPER_API_KEY` + `SCRAPER_API_SITES`) — устарел, остаётся как 3rd priority в chain.
- **Старый ISP zone `pharmacy_monitor`** — оставлен active в Bright Data (фикс $2/мес). Не удалён на случай если понадобится для будущих экспериментов. **TODO**: удалить через UI если решим что Web Unlocker достаточно.

**Just finished (2026-05-27)**: **Phase 0 — defuse time bombs** (commit `af63932`, roadmap ~/.claude/plans/rosy-launching-lamport.md):

- **`APTEKONLINE_CHECKUS` env-var** ([src/scrapers/aptekonline.py](src/scrapers/aptekonline.py)) — захардкоженный bcrypt-токен из `main.js` перенесён в env с back-compat default. Fail-fast только когда переменная *задана но пустая* (защита от .env-опечатки). Default — текущее зафиксированное значение (2026-05-07). На проде env ещё не задан → fallback на default, всё работает. 5 unit-тестов в [tests/test_aptekonline_api.py](tests/test_aptekonline_api.py).
- **Request-ID middleware** ([src/api.py](src/api.py)) — UUID4 per-request, bind в `structlog.contextvars`, response header `X-Request-ID`, Sentry tag через новый `sentry_set_request_id()` в [src/observability.py](src/observability.py). Принимает клиентский `X-Request-ID` если он soft-sanitized (128 ASCII chars max), иначе генерит свежий. 4 unit-теста.
- **Deep `/health` endpoint** ([src/api.py:HealthOut](src/api.py)) — добавлены `db_ping_ms` (`SELECT 1`), `redis_ping_ms` (`PING` если REDIS_URL задан), `sites[]` с `hours_since` per site через `max(Product.last_seen_at)`, `staleness_warning=true` если любой сайт > 30h. Status flips на `"degraded"` при DB unreachable / stale. Backward compat сохранён (старые поля на месте). 3 unit-теста + проверено на проде: aloe=16.6h, aptekonline=5.0h, pharmonline=5.15h.
- **`scripts/cleanup_false_matches.py`** — bulk-rejection 7 known false matches из CSV (`match_id, detach_product_id, reason`). Idempotent через `match_actions.break_match`. 5 unit-тестов. Запускать: `DATABASE_URL=... python -m scripts.cleanup_false_matches data/false_matches.csv --apply` (CSV ещё не подготовлен, нужно идентифицировать 7 пар через UI или SQL).
- **`infra/scripts/cleanup_caddy_backups.sh`** — portable bash (без GNU `find -printf` / `mapfile`), сортирует `.bak.*` по имени (filename = timestamp), удаляет старше N (default 3). Dry-run by default, `--apply` коммитит. Cron suggestion: `0 3 * * 0 ... --apply >> /var/log/caddy-cleanup.log`.

48/48 новых тестов проходят. 9 pre-existing failures в `test_roi.py` (compute_actions signature drift), `test_analytics.py` (NameError), `test_scrapers_snapshot.py` (snapshot fixture out of date) — не от Phase 0, отметить как backlog.

**Just finished (2026-05-26)**: **Full i18n audit + ROI translation**.

- **Locale switcher в сайдбаре**: `LocaleSwitcher` добавлен в `SideNav` ([frontend/src/components/nav.tsx](frontend/src/components/nav.tsx)) над кнопкой logout. Sidebar исправлен на `sticky top-0 h-screen self-start` + `min-h-0` на middle-секции — switcher теперь всегда видим при любой длине меню.
- **AZ locale не переключался**: `POST /api/locale` уходил к FastAPI (Caddy роутит `/api/*` → :8080 → 404). Роут перенесён в `/locale` ([frontend/src/app/locale/route.ts](frontend/src/app/locale/route.ts)). LocaleSwitcher обновлён. Куки устанавливаются через `response.cookies.set()` (не `cookieStore.set()` — ненадёжен в route handlers Next.js 14).
- **71 непереведённая строка** — полный аудит и фикс по всем страницам:
  - `overview/page.tsx` — KPI labels, duration strings (`formatDuration` принимает `t`)
  - `analytics/page.tsx` — все table headers, card titles, empty states, forecast description
  - `site/[site]/page.tsx` — table headers, pagination (prev/next/range), error/empty states, open-link title
  - `comparison/page.tsx` — th_name, th_spread, `{t("sites_spread", {n})}`
  - `notifications-banner.tsx` — все 5 строк через `useTranslations("banner")`
  - `alerts/page.tsx` — tooltip strings
  - Новые ключи добавлены в [frontend/messages/ru.json](frontend/messages/ru.json), [az.json](frontend/messages/az.json), [en.json](frontend/messages/en.json): пространства `analytics`, `banner`, `site`, `overview` (duration), `comparison` (sites_spread), `alerts` (tooltips)
- **ROI action recommendations переведены** (тексты шли из Python backend с hardcoded RU):
  - `src/roi.py`: добавлен `ALL_SITES`, `_STRINGS` (шаблоны title+detail для 4 типов: undercut/price_raise/assortment_gap/promo_response на AZ+EN), функция `translate_action(action, locale)`
  - `src/api.py`: `/api/v1/dash/roi/actions` принимает `?locale=ru|az|en`, применяет `translate_action` при отдаче, кеш `roi_actions_cache` не инвалидируется
  - `frontend/src/lib/api.ts`: `roiActions(client_site?, locale?)` передаёт `?locale=`
  - `overview/page.tsx` + `site/[site]/page.tsx`: `useLocale()` → locale в queryKey + queryFn

**Just finished (2026-05-07)**: HTTPS migration. `leaddrive.cloud` (Namecheap) → 46.225.149.52, Caddy with Let's Encrypt cert. Cookie host-only (no Domain attr) — same-origin, CORS not needed.

**Just finished (2026-05-07)**: AI crawler CLI command `pharmacy-monitor ai-crawl --site <s> --max-urls N [--dry-run] [--budget-usd N]` registered. Config via env: `ANTHROPIC_API_KEY`, `AI_CRAWL_PROVIDER=anthropic`, `AI_CRAWL_MODEL=claude-haiku-4-5`. Deployed on prod. **Sitemap discovery working — aloe returns 19,319 URLs.**

**Just finished (2026-05-07)**: aloe.az extraction fix. Added `parse_next_rsc_jsonld()` in [src/scrapers/ai_crawler.py](src/scrapers/ai_crawler.py) (Layer 1.5 between standard JSON-LD and LLM fallback) — decodes Next.js RSC chunks (`self.__next_f.push([1, "..."])`), unescapes the `__html` value from `dangerouslySetInnerHTML`, parses schema.org Product. Smoke test (50 URLs, dry-run): **25 products extracted, $0 LLM cost**. In Playwright mode the standard `parse_jsonld_product` actually catches it post-hydration (Next.js injects the `<script type="application/ld+json">` after JS runs); RSC parser is a safety net for raw HTML / failed hydration / future HTTP-only fetching. 7 unit tests in [tests/test_ai_crawler.py](tests/test_ai_crawler.py).

**Important corrections to prior architecture notes** (CLAUDE.md was inaccurate):
- **aloe.az is Next.js 13+ App Router with RSC streaming**, not Meteor.
- **pharmonline.az is server-rendered Bootstrap + jQuery**, not Meteor. Existing Playwright DOM scraper [src/scrapers/pharmonline.py](src/scrapers/pharmonline.py) works correctly; no hydration parser needed.

**Just finished (2026-05-09 → 2026-05-11)**: **Diff-only persist + analyzer refactor**. После того как первый Mac прогон (run_45) длился 2ч45 с full persist (100066 snapshots) и потом отчёт ronyalsya N+1 lazy-load'ом, переехали на:

- **`src/main.py:persist_results`** — chunked (`_PERSIST_CHUNK=200`) + diff-only через `_snapshot_payload_changed(last, sp)`. Snapshot пишется **только если цена/discount/promo реально изменились**. `Product.last_seen_at` обновляется всегда — это и есть «видели в этом прогоне».
- **`src/storage.py`** — два общих query helper'а:
  - `latest_snapshots_per_product(session, product_ids) → dict[int, PriceSnapshot]`: текущая цена per product, не привязанная к run_id. Замена `WHERE run_id == last_run` после diff-only.
  - `curr_and_prev_snapshots_for_run(session, current_run) → (curr_snaps, prev_by_product)`: стандартный паттерн для diff-детекторов. Фильтр `Run.started_at < current.started_at` устойчив к близким timestamp'ам.
- **`src/analyzer.py`** — `_detect_price_changes`, `_detect_new_products`, `_detect_undercuts` переписаны под `curr_and_prev_snapshots_for_run` и `latest_snapshots_per_product`. «Новый продукт» = нет snapshot до `run.started_at`. «Текущая цена» = latest globally.
- **Consumers обновлены** под ту же семантику: `src/api.py` (`/dash/comparison`, `/comparisons`), `src/alerts.py` (`_detect_price_drop`, `_detect_new_product`, `_prices_for_match`), `src/roi.py` (`_preload_snapshots`, `_assortment_gaps`), `src/health.py` (`_check_site_drops`, `_check_brand_coverage_drop` — через `Product.last_seen_at`).
- **`src/main.py:_smoke_test_per_site_coverage`** — baseline через `Run.products_scraped` (стабильный счётчик независимо от persist-режима), не `COUNT(snapshots) per run` (после diff-only давал ложные site_drop). Фикс orphan-баг: `AlertEvent` не имеет поля `run_id` — `dedup_key` уже содержит `run={run.id}`.
- **`src/alerts.py:DETECTORS`** — добавлен `"site_drop_smoke": _noop_detector` (events эмитятся из smoke_test напрямую, dispatcher просто пропускает).

Результат на live проде (run_48, 2026-05-09): **1965 snapshots vs 100066 (51× экономия)**, total прогон **1ч15 vs 2ч45**, report_saved без crash, dashboard `/comparison` отрисовывает цены через `latest_snapshots_per_product`. Daily Mac launchd (14:00 UTC) автономно отрабатывает, последние 3 ok-runs подтверждены.

8 diff-only regression-тестов в [tests/test_persist_results.py](tests/test_persist_results.py) + [tests/test_analyzer.py](tests/test_analyzer.py) — **252 теста проходят**.

**Just finished (2026-05-08)**: bulk-persist refactor в [src/main.py `persist_results`](src/main.py). Первый Mac launchd прогон (run_id=40) растянулся на **~5 часов** для **98 536 продуктов** — N+1 SELECT + per-row `session.flush()` через SSH-туннель к prod Postgres. Фикс: pre-fetch existing одной SELECT на категорию (`.in_(external_ids)` чанками по 500), batch `session.add_all()` + единый `flush()` для новых, bulk add snapshots, **commit per result** (bounds транзакции, разгружает WAL). 8 регрессионных тестов в [tests/test_persist_results.py](tests/test_persist_results.py), включая anti-N+1 счётчик SELECT'ов. Развёрнуто на проде. **Этот фикс был промежуточным шагом перед diff-only выше.**

**Just finished (2026-05-07)**: aptekonline JSON API rewrite + split runtime (Mac + prod).
- Aptekonline.az ввёл reCAPTCHA на любые headless-браузеры (даже с Baku-IP). Reverse-engineered backend endpoint `GET https://www.aptekonline.az/shop/productList?categoryId[]=N&lang=az&page=N` (Laravel paginator, 100 items/page) — endpoint найден в `https://www.aptekonline.az/assets/js/main.js?v=35`, работает с заголовком `checkus: $2y$10$...` (статичный bcrypt-токен).
- [src/scrapers/aptekonline.py](src/scrapers/aptekonline.py) полностью переписан: Playwright → httpx (~250 строк). 0.23s/page вместо 30+s. 12 unit-тестов в [tests/test_aptekonline_api.py](tests/test_aptekonline_api.py).
- Hetzner-IP **забанен и на JSON API** aptekonline тоже (HTTP 403 от прода). Поэтому **aptekonline тоже скрейпится с Mac**.

### Runtime layout (обновлено 2026-05-29 — ВСЕ 3 сайта на проде)

**⚠️ Поправка 2026-05-29 (проверено):** запись ниже про «Mac launchd только / прод-таймеры
отключены» УСТАРЕЛА. Реальность: **все 3 сайта скрейпятся с прода**, таймеры enabled и
успешно отрабатывают ежедневно (pharmonline ~01:00, aptekonline ~02:00, aloe ~03:00 UTC;
verified: run_id 123/124, «Finished successfully»). Hetzner-IP бан обойдён residential-прокси:
- **pharmonline** → IPRoyal, DDP WebSocket (`PHARMONLINE_USE_DDP=1`, без браузера). Проверено: `ddp_connected`, 205 категорий.
- **aptekonline** → BrightData residential, httpx JSON. Проверено: `api_loaded total=3267` (403 обойдён).
- **aloe** → direct (не банится).

Mac launchd `com.pharmacy-monitor.scrape` (14:00 UTC) теперь **избыточный DR-fallback** — прод
от ноутбука НЕ зависит (выключен ноут → прод всё равно скрейпит). Можно отключить Mac launchd
(`launchctl unload ~/Library/LaunchAgents/com.pharmacy-monitor.scrape.plist`) чтобы убрать
двойной скрейп, либо оставить как бесплатный (Baku-direct) запасной источник.

---
_Историческая запись (устарела, см. поправку выше):_ Гибрид: **aloe** на проде (direct, 03:00 UTC), **pharmonline + aptekonline** с Mac launchd (14:00 UTC = 18:00 Asia/Baku). Прод-таймер pharmonline отключён 2026-05-11. Aptekonline-таймер на проде отключён 2026-05-08.

| Сайт | Где | Чем | Расписание |
|---|---|---|---|
| **aloe.az** | Hetzner prod | Playwright DOM (см. [src/scrapers/aloe.py](src/scrapers/aloe.py)) | systemd timer `pharmacy-monitor-scrape@aloe` 03:00 UTC, direct |
| **pharmonline.az** | **Прод (Hetzner) — DDP + IPRoyal** | Meteor DDP WebSocket (см. [src/scrapers/pharmonline_ddp.py](src/scrapers/pharmonline_ddp.py)) — pierces Cloudflare без браузера, через IPRoyal residential proxy ($1.75/GB). | systemd timer `pharmacy-monitor-scrape@pharmonline` 01:00 UTC. Активирован 2026-05-27 после run 104=ok с 266,718 products. Reconnect-on-close logic выживает persist phase. Mac launchd теперь DR-fallback только: `bash infra/local/run-scrape.sh --site pharmonline --site aptekonline`. |
| **aptekonline.az** | **Mac launchd только** | httpx JSON API (см. [src/scrapers/aptekonline.py](src/scrapers/aptekonline.py)) | Mac launchd `com.pharmacy-monitor.scrape` 18:00 Asia/Baku. **Прод-таймер отключён 2026-05-08** (`systemctl disable --now pharmacy-monitor-scrape@aptekonline.timer`) — ScraperAPI default pool отдаёт HTTP 403, нужен residential (Hobby $49/мес). Endpoint: `GET /shop/productList?categoryId[]=N&lang=az&page=N` (Laravel paginator), header `checkus: $2y$10$...` из `main.js`. 12 тестов в [tests/test_aptekonline_api.py](tests/test_aptekonline_api.py). |

Systemd unit на проде: `/etc/systemd/system/pharmacy-monitor-scrape@.service`, ExecStart=`pharmacy-monitor run --site %i --mode category`. Активны таймеры **pharmonline + aloe** (aptekonline отключён 2026-05-08).

Mac launchd: [infra/local/com.pharmacy-monitor.scrape.plist](infra/local/com.pharmacy-monitor.scrape.plist) → [infra/local/run-scrape.sh](infra/local/run-scrape.sh). `DEFAULT_ARGS=--site pharmonline --site aptekonline --mode category --no-alerts`. Открывает SSH-туннель Mac:5433 → prod:5432, тянет PG_PASS из Keychain (`security add-generic-password -a pm -s pharmacy-monitor-db -w '<pwd>'`). Логи: `~/Library/Logs/pharmacy-monitor.log`. Управление: `launchctl load|unload|start ~/Library/LaunchAgents/com.pharmacy-monitor.scrape.plist`.

ScraperAPI: `SCRAPER_API_KEY`, `SCRAPER_API_SITES=pharmonline,aptekonline` в `/etc/pharmacy-monitor/env` (для aptekonline теперь не используется — таймер выключен, но env-переменная безвредна). Free trial 5K credits/мес. Для возврата aptekonline на прод нужен **Hobby $49/мес (residential pool)**.

## Known issues / NOT done

- ~~Resend SMTP not configured~~ **CONFIGURED & WORKING** (2026-05-29 verified): `SMTP_HOST=smtp.resend.com`, verified domain `digest@leaddrive.cloud`. Daily digest email delivered to `EMAIL_TO` (rashadrahimov@gmail.com) — see journald `smtp_sent_ok` / `digest_sent` (digest@daily.timer 05:00 UTC).
- **Weekly digest (added 2026-05-31)**: `pharmacy-monitor-digest-weekly.timer` → **Mondays 06:00 UTC (10:00 Baku)**, runs `notify digest weekly` (7-day window → `tenant_users` with `weekly_digest=True`; admin id=1 opted in). Delivers via the same SMTP. Distinct from the daily `EMAIL_TO` digest (left untouched). Units: `infra/systemd/pharmacy-monitor-digest-weekly.{service,timer}`. NB: empty-week → no email (digest skips when 0 events in window).
- Telegram bot token NOT configured (`TELEGRAM_BOT_TOKEN` empty) — push-alerts off; email digest covers delivery. Activate via `configure-integrations.sh` → @BotFather token + chat_id.
- ~~Sentry DSN not configured~~ **CONFIGURED & WORKING** (2026-05-29 verified): `SENTRY_DSN` set, `init_sentry()` runs on startup (journald `sentry_initialized env=production`). FastAPI+SQLAlchemy integrations.
- NOTE: `pharmacy-monitor notify test` для smoke-теста доставки запускать с загруженным env (systemd EnvironmentFile НЕ грузится при ручном CLI): `set -a; source /etc/pharmacy-monitor/env; .venv/bin/pharmacy-monitor notify test`.
- ~~22317 AZN bug in aptekonline price parser~~ FIXED 2026-05-07. Root cause: aptekonline's Angular template `'<del>' + price + 'AZN </del>' + p.discount_price + ' AZN '` renders with no separator, so `inner_text` of `.new-price` returns e.g. `"22AZN 317 AZN"` for a discounted product. Old `parse_price` stripped non-digits → `"22317"`. Fix: extract only the FIRST digit-run-with-dots/commas via regex. Existing bad rows перезатираются следующим aptekonline-прогоном (Mac launchd 18:00 Asia/Baku ежедневно); для немедленной очистки: `DELETE FROM price_snapshots WHERE site='aptekonline' AND price > 5000;`
- pharmonline.az has NO `/sitemap.xml` (returns SPA HTML); aptekonline returns empty `<urlset>` — both need BFS fallback (regular Playwright scrapers continue to work via category pages)
- **Hetzner DE IP banned by pharmonline.az + aptekonline.az** — частично обойдено:
  - **pharmonline → IPRoyal (DDP)** на проде — ✅ работает, daily.
  - **aloe → direct** на проде — ✅ работает.
  - **aptekonline → ТОЛЬКО Mac (Baku-IP)** — ⚠️ **прод-прокси НЕ работают (проверено 2026-06-02, перепробованы ВСЕ 3)**:
    - **IPRoyal**: без гео aptek→`403` (нужен AZ-IP); с `country=az`→IPRoyal сам `407` (**нет AZ-residential пула**).
    - **BrightData**: `407 Account is suspended` (аккаунт приостановлен, биллинг).
    - **ScraperAPI**: с `country=az` aptek→`403` (az-гео не даёт рабочий AZ-IP).
    - **Корень:** aptek требует НАСТОЯЩИЙ азербайджанский residential IP; ни один провайдер не выдаёт. Единственный рабочий — реальный **Baku-IP с Mac**.
    - **Прод-таймер aptek ОТКЛЮЧЁН** (`systemctl disable pharmacy-monitor-scrape@aptekonline.timer`) — иначе 0 товаров + 325 critical-алертов/прогон.
    - **Рабочий путь:** Mac launchd `com.pharmacy-monitor.scrape` 18:00 Baku, через SSH-туннель Mac:5433→prod:5432. Туннель `com.pharmacy-monitor.db-tunnel` ДОЛЖЕН быть жив (падал → aptek простаивал 6 дней 2026-05-26→06-01, чинится перезапуском). Mac должен быть включён.
    - **NB:** матчинг-фаза Mac-скрейпа ~3-4ч (каждый DB-query через туннель = latency; на сервере 2 мин). Отрабатывает за ночь — ок.
    - **Чтобы снять зависимость от Mac:** нужен прокси с **AZ-residential** пулом (IPRoyal/ScraperAPI/BrightData его НЕ имеют для aptek) ИЛИ разблокировать BrightData-биллинг (но AZ-пул у них не гарантирован). До тех пор — aptek = Mac.
  - (Прежние записи «RESOLVED via BrightData / Mac-зависимость снята» — УСТАРЕЛИ, см. строки ~151/328/342.)
- Project under git с 2026-05-11. Initial commit `c7fde84` зафиксировал diff-only state. **Remote**: `origin` = `https://github.com/rashadrahimov/pharmacy-monitor.git`. Auth работает через cached creds (`git push origin main` без проблем).
- ~~forecast.py не рефакторен под diff-only~~ **DONE 2026-05-28**: `compute_trend` имеет Case A/B/C для sparse data (0 snaps в окне → latest globally; 1-2 snaps same price → stable). `top_movers` имеет pre-cutoff lookup для single-snapshot products. 3 diff-only regression теста в `tests/test_forecast.py` (`test_compute_trend_diff_only_sparse_active_pricing`, `test_predict_competitor_moves_diff_only_skips_truly_stable`, `test_top_movers_diff_only_sparse_change`). 18/18 forecast тестов проходят.
- 7 false matches in matcher (Friso 3 Gold ↔ Friso Prematures etc) — needs manual reject via UI on /comparison
- `admin off` in `/etc/caddy/Caddyfile` — `systemctl reload caddy` fails, use `restart` instead
- Caddy backup config: `/etc/caddy/Caddyfile.bak.20260506-2038` (pre-HTTPS)
- ~~**Next.js standalone deploy gotcha (2026-05-11)**: после `pnpm build` руками копировать static в standalone~~ **FIXED 2026-05-11**: `ExecStartPre` в `/etc/systemd/system/pharmacy-monitor-frontend.service` теперь автоматически копирует `.next/static/` → `.next/standalone/.next/static/` при каждом restart. Деплой свёлся к: `tar czf - <files> | ssh ... 'tar xzf -' && ssh ... 'cd frontend && pnpm build && systemctl restart pharmacy-monitor-frontend'`. **Важно: владелец `.next/` должен быть `pm:pm`** (chown'нили 2026-05-11) — иначе ExecStartPre упадёт на `Permission denied`.
- **Прод-деплой = RSYNC, НЕ git (важно, 2026-05-30):** `/opt/pharmacy-monitor` на проде — rsync-снимок, рабочего `.git` там НЕТ (был stale worktree-указатель на Mac-путь `gitdir: /Users/.../worktrees/...`, удалён 2026-05-30). Git-based `infra/deploy.sh` (`git reset --hard origin/main`) на проде НЕ работает. Деплой = `rsync src/ + frontend/` → `pnpm build` → `systemctl restart` (см. `.github/workflows/deploy.yml`, переписан под rsync 2026-05-30). CI-секрет `SSH_PRIVATE_KEY` — **pm-scoped** (root@ в CI = Permission denied); deploy.yml требует одноразовый sudoers-дроп для pm на 2 restart'а.
- **Alembic на проде — КОНСИСТЕНТЕН (уточнено 2026-05-30):** app-БД (Postgres `pharmacy_monitor`) `alembic_version` = `0008_pricing_config`, и схема всех 0006–0009 присутствует (alert-cols, `products.barcode`+index, `pricing_config`, `url_dead_at` — всё есть, проверено). Ранняя «паника про 0005» была артефактом: `alembic current` БЕЗ `DATABASE_URL` падал на stale fallback-SQLite. **`alembic upgrade head` БЕЗОПАСЕН** (0009 идемпотентна, guard на existing column). Единственный нюанс: 0009 не застамплена на PG (version=0008) — безвредно, само поправится при ближайшем `upgrade`. Ручной стамп (опц.): `sudo -u postgres psql pharmacy_monitor -c "UPDATE alembic_version SET version_num='0009_product_url_dead_at'"`.
- **az/aloe ярлыки категорий переведены (2026-05-30):** 303 `Category.label_ru` (aptekonline+aloe мед-таксономия) были на азербайджанском → переведены az→ru и записаны в прод-БД (транзакция). Источник: `data/category_labels_competitors_ru.json` ({id,az,ru}). Бэкап старых значений: таблица `categories_label_bak_20260530` (id, old_label_ru). Откат: `UPDATE categories c SET label_ru=b.old_label_ru FROM categories_label_bak_20260530 b WHERE c.id=b.id;`. Остались на латинице 4 бренда (La Roche-Posay/Vichy/Biolane/Berdoues — намеренно). Ярлыки читаются из БД вживую → ru-дашборд показывает сразу.

## Useful commands

```bash
# Local dev
docker compose up -d                        # Postgres + Redis
.venv/bin/uvicorn src.api:app --reload      # API
cd frontend && pnpm dev                     # Next.js
.venv/bin/pytest -q                         # 80 unit tests

# Deploy
rsync -avz -e "ssh -i ~/.ssh/id_ed25519" \
  --exclude='.venv/' --exclude='node_modules/' --exclude='.next/' \
  --exclude='__pycache__/' --exclude='.git/' --exclude='data/db.sqlite*' \
  ./ "pm@46.225.149.52:/opt/pharmacy-monitor/"

# Sync deps after pyproject.toml change (server has no `uv` and no `pip` in venv —
# use ensurepip + python -m pip):
ssh -i ~/.ssh/id_ed25519 -l root 46.225.149.52 \
  'cd /opt/pharmacy-monitor && .venv/bin/python -m ensurepip --upgrade && \
   .venv/bin/python -m pip install -q -e .'

# Server ops (root user — pm has no passwordless sudo)
ssh -i ~/.ssh/id_ed25519 -l root 46.225.149.52
systemctl restart pharmacy-monitor-api       # restart backend
systemctl restart pharmacy-monitor-frontend  # restart frontend
systemctl restart caddy                      # restart proxy (reload fails — admin off)
systemctl start pharmacy-monitor-scrape@pharmonline  # manual scrape

# DB inspection (root)
ssh -i ~/.ssh/id_ed25519 -l root 46.225.149.52
sudo -u postgres psql pharmacy_monitor
\dt
SELECT COUNT(*) FROM products;
```

## Code style preferences

- Python 3.12+ syntax (T | None, not Optional[T])
- Russian comments allowed for business logic, English for infra/migrations
- Tests live in `tests/`, named `test_<module>.py`
- Avoid `print()` in src/ — use `structlog`
- New endpoints under `/api/v1/dash/*` for frontend, `/api/v1/<x>` for ERP

## Important context I should not forget

- User is in Baku (Asia/Baku timezone, UTC+4)
- User speaks Russian primarily, prefers concise communication
- User trusts auto mode — minimize confirmation prompts
- Hetzner project ID: `14487088` (`pharmacy-monitor`)
- API token revoked after deploy — to create another: console.hetzner.com → Security → API Tokens

## Roadmap state (12-week plan, all in docs/PRODUCTION_OVERVIEW.md)

```
[✓] W1-W11      All foundation + monitoring + i18n done
[✓] W12         i18n + load test + go-live runbook  
[✓] Deployed    Hetzner production live with 17288 rows of pilot data
[✓] HTTPS       leaddrive.cloud + www, Let's Encrypt auto-renew (2026-05-07)
[✓] AI crawler  CLI shipped, sitemap works, aloe extraction working (RSC JSON-LD + Playwright JSON-LD), 25/50 products on dry-run smoke
[✓] Nightly     Hybrid runtime: aloe на проде (03:00 UTC), pharmonline+aptekonline с Mac launchd 18:00 Baku
[✓] Diff-only   persist + analyzer + 5 consumers под новую семантику (2026-05-09 → 11). 51× экономия snapshots, 2× быстрее прогон.
[✓] Git         project under VCS с 2026-05-11 (initial commit c7fde84) + remote github.com/rashadrahimov/pharmacy-monitor
[✓] i18n full   locale switcher в nav, AZ/EN fix (route /locale вместо /api/locale), 71 строка переведена, ROI actions переведены (2026-05-26)
[✓] Phase 0     defuse time bombs: env-checkus, request_id, deep /health, cleanup tools (2026-05-27, commit af63932)
[~] Phase 1     scraper resilience: BD Web Unlocker для aptekonline ✓, pharmonline даёт 502 от BD → остался на Mac. AI fallback wired (opt-in)
[✓] Phase 1c    DDP scraper для pharmonline (commit 6969e96+beac3ca). Reverse-engineered Meteor `products` method, pierces Cloudflare без браузера через IPRoyal residential. Reconnect-on-close logic — выживает persist phase pause. **Run 104: status=ok, 266,718 products** (vs 599 без reconnect = 445× прирост).
[✓] Cutover     pharmonline переведён на прод-таймер (01:00 UTC = 05:00 Baku). Mac launchd теперь скрейпит только aptekonline. Mac DR-fallback для pharmonline остался (`bash run-scrape.sh --site pharmonline`).
[✓] Phase 2.5   Match suggestion review UI (commit 82922ad). GET /api/v1/dash/matches/suggestions + POST /confirm. Page /matches/review с confidence slider + needs_review filter + confirm/reject buttons. 8 unit tests pass. i18n ru/az/en.
[✓] Phase 3.3   Local backup восстановлен (commit 437dfc9 — фикс `$2: unbound variable` regression от 2026-05-23) + GPG encryption. Manual offsite через `infra/local/fetch-backup.sh` на Mac. B2 cloud отложен по решению клиента.
[✓] Phase 2     matcher v2 с barcode (2.1-2.4): migration 0007, extraction в aloe+pharmonline, priority-0 pass, rematch script. UI 2.5 deferred.
[✓] CI/CD       CI-гейт зелёный (2026-05-30): ci-pipeline.yml = ruff check+format / pytest (вкл. /price-index 200) / frontend tsc+build. deploy.yml ПЕРЕДЕЛАН под реальность прода: workflow_dispatch + rsync (прод НЕ git-чекаут!) + pm-ключ SSH_PRIVATE_KEY + pnpm build + restart (пререквизит: pm sudoers на 2 restart'а); алембик НЕ катит авто (drift на 0005). Аудит-фиксы (бэкенд) + mobile nav (фронт) ЗАДЕПЛОЕНЫ на прод 2026-05-30 вручную rsync'ом (deploy.sh git-метод не сработал — прод не git).
[ ] Phase 3     HA & backups: Postgres replica + B2 offsite backup
[ ] Next        Phase 2.5 (UI suggestion queue), Phase 3, либо real-data barcode coverage analysis после нескольких daily scrape
```

### Out of scope (decided 2026-05-07 by client)

- **ERP интеграция клиента** — не требуется. Не предлагать в будущих сессиях.
- **PWA / native mobile app** — не требуется. Текущий Next.js mobile-first дашборд достаточен.

## Integration setup (one-shot)

Для настройки SMTP / Telegram / Sentry / GitHub remote / ScraperAPI Hobby:

```bash
bash scripts/configure-integrations.sh
```

Интерактивный скрипт — 5 блоков, каждый можно пропустить. Обновляет
`/etc/pharmacy-monitor/env` на проде, рестартит `pharmacy-monitor-api`.
Ссылки на signup-страницы каждого сервиса встроены в подсказки.

## How to continue work

1. Read this file (you already are)
2. Check `docs/PRODUCTION_OVERVIEW.md` for full architecture
3. Check `docs/GO_LIVE.md` for deployment runbook
4. Check `~/.claude/plans/buzzing-greeting-wilkes.md` — audit от 2026-05-11
5. `git log --oneline` — история коммитов (initial snapshot `c7fde84` 2026-05-11)
