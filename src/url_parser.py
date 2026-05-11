"""Парсинг URL категорий 3 сайтов в (site, slug).

Используется в `🗂️ Категории` дашборда — клиент вставляет полные URL,
система автоматически извлекает slug нужного формата для каждого скрейпера.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

# Regex'ы определяют site по доменy + извлекают slug. ?: для not-capturing групп.
PATTERNS: list[tuple[str, re.Pattern]] = [
    # pharmonline.az/products?category=ushaq-qidasi  (с www. или без)
    (
        "pharmonline",
        re.compile(
            r"^https?://(?:www\.)?pharmonline\.az/products\?category=([^&#]+)",
            re.IGNORECASE,
        ),
    ),
    # aptekonline.az/products/252  (числовой id)
    (
        "aptekonline",
        re.compile(
            r"^https?://(?:www\.)?aptekonline\.az/products/(\d+)",
            re.IGNORECASE,
        ),
    ),
    # aloe.az/catalog/filters/?category_slug=uşaq-qidası  (или url-encoded)
    (
        "aloe",
        re.compile(
            r"^https?://(?:www\.)?aloe\.az/catalog/filters/\?category_slug=([^&#]+)",
            re.IGNORECASE,
        ),
    ),
    # aloe.az/catalog/filters/?product_field=bestseller  (специальные срезы)
    (
        "aloe",
        re.compile(
            r"^https?://(?:www\.)?aloe\.az/catalog/filters/\?product_field=([^&#]+)",
            re.IGNORECASE,
        ),
    ),
]


def parse_category_url(url: str) -> tuple[str | None, str | None]:
    """Распарсить URL → (site, slug). Возвращает (None, None) если URL неизвестного формата.

    Примеры:
        >>> parse_category_url("https://pharmonline.az/products?category=ushaq-qidasi")
        ('pharmonline', 'ushaq-qidasi')
        >>> parse_category_url("https://aptekonline.az/products/252")
        ('aptekonline', '252')
        >>> parse_category_url("https://aloe.az/catalog/filters/?category_slug=u%C5%9Faq-qidas%C4%B1")
        ('aloe', 'uşaq-qidası')
        >>> parse_category_url("https://aloe.az/catalog/filters/?product_field=bestseller")
        ('aloe', 'product_field=bestseller')
    """
    if not url:
        return None, None
    url = url.strip()
    for site, pattern in PATTERNS:
        m = pattern.search(url)
        if m:
            raw_slug = m.group(1)
            # url-decode турецкие символы и проч.
            slug = unquote(raw_slug)
            # обрезаем trailing slash если он попал в slug
            slug = slug.rstrip("/")
            # Для product_field= паттерна aloe — храним как product_field=VALUE,
            # т.к. наш AloeScraper ожидает именно такой формат
            if "product_field=" in pattern.pattern:
                slug = f"product_field={slug}"
            return site, slug
    return None, None


def parse_urls_block(text: str) -> tuple[dict[str, str], list[str]]:
    """Распарсить многострочный блок URL'ов в (found, unknown).

    Если попалось >1 URL для одного сайта — побеждает последний.

    Returns:
        ({"pharmonline": "...", "aptekonline": "...", "aloe": "..."}, ["неузнанная_строка1", ...])
    """
    found: dict[str, str] = {}
    unknown: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        site, slug = parse_category_url(line)
        if site and slug:
            found[site] = slug
        else:
            unknown.append(line)
    return found, unknown
