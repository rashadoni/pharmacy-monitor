# ADR-001: Multi-tenant skeleton остаётся dormant

**Дата:** 2026-04-29
**Статус:** Accepted
**Контекст:** Pharmacy Monitor одному клиенту (pharmonline) без планов на 2-го клиента в обозримом будущем.

## Решение

**Multi-tenant НЕ внедряется** в query scope, **НО skeleton не удаляется**.

Конкретно:

| Что есть | Решение |
|---|---|
| `tenant_id INTEGER DEFAULT 1` в 10 таблицах | Оставляем — цена 0 KB, удалять = ALTER TABLE риск |
| `Tenant` + `TenantUser` таблицы | Оставляем — работают для magic-link auth ([src/auth.py](../src/auth.py)) |
| `src/tenants.py` модуль (CRUD + magic-link) | Оставляем — используется в auth.py |
| `pharmacy-monitor tenant ...` CLI команды | Оставляем — пригодятся когда добавится 2-й клиент |
| `WHERE tenant_id = current` в queries | **НЕ внедряется** |
| Tenant switcher в дашборде | Оставлен (показывается только если >1 тенанта) |

## Почему

### Про DROP колонок:
1. SQLite не поддерживает `DROP COLUMN` на старых версиях (требуется ≥3.35)
2. Колонки занимают ~4 байта/строка × 10 таблиц = тривиально
3. Удаление = риск SQL ошибок без выгоды

### Про query scope:
1. Реализация = 14+ файлов (analyzer, matcher, alerts, inventory, roi, ...) с фильтром `WHERE tenant_id = current`
2. ~3-4 часа риск-рефактора
3. Без 2-го клиента → нет верификации что работает
4. Когда придёт 2-й клиент — лучше делать с боевыми данными

### Что использует skeleton сейчас (не зря)

- `src/auth.py` использует `tenants.add_user / verify_magic_token` для magic-link login
- `current_tenant_id()` пока всегда возвращает 1 — это OK для одного клиента

## Когда вернуться к этому решению

Триггер для внедрения query scope:
- 🟢 Контракт с 2-м клиентом (любой аптекой)
- 🟢 ИЛИ внутренний sandbox-tenant для демо
- 🟢 ИЛИ запрос pharmonline на multi-user roles (admin/viewer)

Тогда план:
1. Создать второго `Tenant` через `pharmacy-monitor tenant add`
2. Один за одним проходить по 14 файлам, добавлять `WHERE tenant_id = current_tenant_id()`
3. Тесты с 2 тенантами на in-memory SQLite
4. Verify isolation через CLI / дашборд

## Альтернативы рассмотренные

1. **DROP всё что связано с tenant** — отвергнуто: убираем foundation, лишь чтобы не "висело"
2. **Implement query scope сейчас** — отвергнуто: см. "Почему" выше
3. **Текущее решение (dormant)** — accepted

## Последствия

- ✅ Код не блокируется на refactor'е без необходимости
- ✅ Magic-link auth работает (использует `Tenant`/`TenantUser`)
- ✅ Когда придёт 2-й клиент — фундамент готов
- ⚠️ Если кто-то в будущем добавит ещё `Tenant` записи и не fix'нет queries — данные будут смешаны (пометка в коде есть)
