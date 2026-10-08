-- Опустить нижнюю границу каталога pharmonline. Только по слову владельца.
--
-- Что это за граница, когда её опускают и до какого значения —
-- docs/RUNBOOK.md, «Нижняя граница каталога pharmonline».
--
-- Запуск с dev-бокса (подставить два числа: границу, которая стоит сейчас, и
-- новую):
--
--   ssh root@13.140.186.143 "sudo -u postgres env \
--     PGOPTIONS='-c pm.expect_floor=9000 -c pm.new_floor=8900' \
--     psql -X -v ON_ERROR_STOP=1 pharmacy_monitor" \
--     < scripts/lower_pharmonline_catalog_floor.sql
--
-- Скрипт только опускает, за один раз не больше чем на десятую часть, и
-- отказывает, если граница в базе не та, что названа в pm.expect_floor. Отказ —
-- это ошибка и нетронутая таблица. Прежнее значение печатается: вернуть его
-- можно только руками (UPDATE), этим скриптом граница не поднимается.
--
-- Здесь обычный SQL без мета-команд psql: параметры приходят настройками
-- сеанса, поэтому tests/test_lower_pharmonline_catalog_floor.py исполняет файл
-- тем же драйвером, что и приложение.

DO $floor$
DECLARE
    v_tenant CONSTANT integer := 1;
    v_expect integer;
    v_new integer;
    v_current integer;
    v_rows integer;
BEGIN
    BEGIN
        v_expect := nullif(current_setting('pm.expect_floor', true), '')::integer;
        v_new := nullif(current_setting('pm.new_floor', true), '')::integer;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'pm.expect_floor и pm.new_floor должны быть целыми числами';
    END;
    IF v_expect IS NULL OR v_new IS NULL THEN
        RAISE EXCEPTION 'не заданы pm.expect_floor и pm.new_floor — см. шапку файла';
    END IF;

    -- Строки держим до конца транзакции, чтобы два запуска скрипта не
    -- наложились: второй дождётся первого и откажет на сверке pm.expect_floor.
    -- Сбору это не мешает: он границу только читает и видит либо прежнее
    -- значение, либо новое.
    PERFORM 1
    FROM pharmonline_public_api_catalog_baselines
    WHERE tenant_id = v_tenant
    FOR UPDATE;

    -- Действующая граница — наибольшее значение среди строк тенанта: так её
    -- читает сбор (main._verify_pharmonline_public_api_identities).
    SELECT max(minimum_catalog_item_count)
    INTO v_current
    FROM pharmonline_public_api_catalog_baselines
    WHERE tenant_id = v_tenant;

    IF v_current IS NULL THEN
        RAISE EXCEPTION 'границы нет: в pharmonline_public_api_catalog_baselines нет строк тенанта %', v_tenant;
    END IF;
    IF v_current <> v_expect THEN
        RAISE EXCEPTION 'граница сейчас %, а названа % — перечитайте таблицу и повторите', v_current, v_expect;
    END IF;
    IF v_new < 1 OR v_new >= v_current THEN
        RAISE EXCEPTION 'граница только опускается: сейчас %, запрошено %', v_current, v_new;
    END IF;
    IF v_new * 10 < v_current * 9 THEN
        RAISE EXCEPTION 'слишком большой шаг: с % до % — больше десятой части (опечатка?)', v_current, v_new;
    END IF;

    UPDATE pharmonline_public_api_catalog_baselines
    SET minimum_catalog_item_count = v_new
    WHERE tenant_id = v_tenant
      AND minimum_catalog_item_count > v_new;
    GET DIAGNOSTICS v_rows = ROW_COUNT;

    RAISE NOTICE 'нижняя граница каталога pharmonline: % -> % (строк: %)', v_current, v_new, v_rows;
END
$floor$;

SELECT id, tenant_id, catalog_item_count, minimum_catalog_item_count, created_at
FROM pharmonline_public_api_catalog_baselines
ORDER BY id;
