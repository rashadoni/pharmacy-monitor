"""Ритм сбора per-site — единственный источник правды о расписании скрейпа.

Все пороги «данные устарели» в проекте выводятся отсюда, а не прописываются
по сайтам руками. Карта обязана совпадать с systemd-таймерами на проде:
``infra/systemd/overrides/pharmacy-monitor-scrape@<site>.timer.d/zz-*-cadence.conf``.

Зачем отдельный модуль: расписание нужно и health-чекам (`src/health.py`), и
политике доверия каталогу (`src/product_policy.py`). Общий низкоуровневый
модуль без зависимостей избавляет от импорта одного в другой.

Рассинхрон карты с таймерами уже стоил ложной тревоги: 2026-10-04 aptekonline
перевели на недельный ритм (его Cloudflare обходится только платным выходом,
ежедневный сбор ≈$149/мес против $49 за недельный), а пороги остались
суточными — дашборд красил сайт красным 6 дней из 7, хотя он собирался ровно
по графику. **Меняешь таймер — меняй и эту карту.**
"""

from __future__ import annotations

import math

SITE_SCRAPE_CADENCE_HOURS: dict[str, int] = {
    "pharmonline": 24,
    "aptekonline": 168,  # недельный ритм, решение владельца 2026-10-04
    "aloe": 24,
}
_DEFAULT_CADENCE_HOURS = 24

# Запас поверх ритма на jitter/retry и длительность самого прогона: данные
# считаются устаревшими только когда пропущен ЦЕЛЫЙ запуск плюс этот запас.
CADENCE_GRACE_HOURS = 6


def site_cadence_hours(site: str) -> int:
    """Ожидаемый интервал между полными сборами сайта (часы)."""
    return SITE_SCRAPE_CADENCE_HOURS.get(site, _DEFAULT_CADENCE_HOURS)


def site_max_age_hours(site: str) -> int:
    """Возраст, после которого данные сайта «просрочены»: ритм + запас.

    Суточный сайт → 30ч, недельный → 174ч.
    """
    return site_cadence_hours(site) + CADENCE_GRACE_HOURS


def site_cadence_days(site: str) -> int:
    """Ритм сбора в днях, вверх: суточный → 1, недельный → 7."""
    return max(1, math.ceil(site_cadence_hours(site) / 24))
