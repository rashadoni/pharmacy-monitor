"""Сбор Kontakt Home через настоящий Chrome (`src/scrapers/kontakt_browser.py`)
и файл для `catalog import` (`src/kontakt_collect.py`).

Chrome здесь не запускается: запрос изнутри вкладки (`_page_fetch`) подменён
словарём страниц. Проверяется то, что зависит от нашего кода: исходы запроса
(страница, проверка Cloudflare, сбой), то, что файл принимает сервер тем же
путём, что файл, собранный руками, и что товар узнаётся по тому же
идентификатору, что в загрузке 2026-11-13 (адрес страницы, не артикул).
"""

from __future__ import annotations

import csv
from urllib.parse import parse_qs, urlsplit

import pytest

from src import catalog_file, kontakt_collect
from src.scrapers import kontakt_browser
from src.scrapers.base import CaptchaDetected
from src.scrapers.http_catalog import PageFetchError
from src.scrapers.kontakt_browser import KontaktBrowserScraper
from tests.test_electronics_scrapers import (
    CLOUDFLARE_CHALLENGE,
    kontakt_item,
    kontakt_listing,
    turbo_encode,
)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(kontakt_browser.asyncio, "sleep", instant)


def browser(answers, tmp_path):
    """Сборщик, у которого вкладка отвечает из `answers` (функция адреса)."""
    scraper = KontaktBrowserScraper(tmp_path / "profile", rate_limit_sec=0)
    seen: list[str] = []

    async def page_fetch(url):
        seen.append(url)
        return answers(url)

    scraper._page_fetch = page_fetch
    return scraper, seen


def sequence(*answers):
    it = iter(answers)
    return lambda _url: next(it)


async def test_page_is_returned_as_the_http_scraper_would(tmp_path):
    scraper, seen = browser(sequence({"status": 200, "text": "data"}), tmp_path)
    response = await scraper.fetch("https://kontakt.az/tv.data", params={"_routes": "routes/$"})
    assert (response.status_code, response.text) == (200, "data")
    assert parse_qs(urlsplit(seen[0]).query) == {"_routes": ["routes/$"]}


@pytest.mark.parametrize(
    "answer",
    [
        {"status": 200, "text": CLOUDFLARE_CHALLENGE},
        {"status": 403, "text": CLOUDFLARE_CHALLENGE},
        {"status": 403, "text": "forbidden"},
    ],
)
async def test_a_check_or_refusal_stops_at_once_and_is_not_retried(tmp_path, answer):
    scraper, seen = browser(lambda _url: answer, tmp_path)
    with pytest.raises(CaptchaDetected):
        await scraper.fetch("https://kontakt.az/tv.data")
    assert len(seen) == 1


async def test_transient_answer_is_retried_and_counted(tmp_path):
    scraper, seen = browser(
        sequence({"status": 502, "text": ""}, {"status": 200, "text": "ok"}), tmp_path
    )
    assert (await scraper.fetch("https://kontakt.az/tv.data")).text == "ok"
    assert len(seen) == 2 and scraper.fetch_retries == 1


async def test_a_permanent_answer_is_not_retried(tmp_path):
    scraper, seen = browser(lambda _url: {"status": 404, "text": "yoxdur"}, tmp_path)
    with pytest.raises(PageFetchError) as error:
        await scraper.fetch("https://kontakt.az/yoxdur.data")
    assert error.value.status == 404 and len(seen) == 1


async def test_a_network_failure_inside_the_tab_is_not_called_protection(tmp_path):
    scraper, seen = browser(lambda _url: {"status": None, "error": "TypeError"}, tmp_path)
    with pytest.raises(PageFetchError) as error:
        await scraper.fetch("https://kontakt.az/tv.data")
    assert "network error" in error.value.reason
    assert len(seen) == scraper.max_retries


@pytest.mark.parametrize(
    "url", ["https://example.com/x.data", "http://kontakt.az/x.data", "https://kontakt.az.evil/x"]
)
async def test_only_kontakt_az_is_ever_requested(tmp_path, url):
    scraper, seen = browser(lambda _url: {"status": 200, "text": "x"}, tmp_path)
    with pytest.raises(PageFetchError):
        await scraper.fetch(url)
    assert seen == []


def test_no_masking_of_the_browser():
    """Решение владельца: Chrome как есть — без подмены отпечатка и признаков."""
    with open(kontakt_browser.__file__, encoding="utf-8") as fh:
        source = fh.read()
    # Приёмы маскировки в Playwright: подмена заголовка и признака
    # автоматизации, скрипт до загрузки страницы, плагины stealth.
    for usage in (
        "user_agent=",
        "add_init_script",
        "ignore_default_args",
        "disable-blink-features",
        "import playwright_stealth",
        "from playwright_stealth",
        "navigator.webdriver",
    ):
        assert usage not in source, usage
    assert 'channel="chrome"' in source


# -- полный сбор и файл ------------------------------------------------------------

MENU = {
    "root": {
        "data": {
            "categories": [
                {"url_path": "tv", "is_category": True, "children": []},
                {"url_path": "phones", "is_category": True, "children": []},
            ]
        }
    }
}


def site(pages):
    def answer(url):
        path = urlsplit(url).path
        page = parse_qs(urlsplit(url).query).get("p", ["1"])[0]
        key = path if page == "1" else f"{path}?p={page}"
        if key not in pages:
            return {"status": 404, "text": ""}
        return {"status": 200, "text": turbo_encode(pages[key])}

    return answer


async def test_collect_writes_a_file_the_server_accepts_with_the_old_identity(tmp_path):
    sale = kontakt_item(sku="A-1", regular=100, final=80)
    plain = kontakt_item(sku="B-2", regular=50, final=50, in_stock=False)
    pages = {
        "/_root.data": MENU,
        "/tv.data": kontakt_listing([sale, plain], 2),
        # Тот же товар и во второй категории: в файле — один раз.
        "/phones.data": kontakt_listing([kontakt_item(sku="A-1", regular=100, final=80)], 1),
    }
    scraper, _ = browser(site(pages), tmp_path)
    rows = await kontakt_collect.collect(scraper)
    out = tmp_path / "kontakt.csv"
    kontakt_collect.write_catalog(rows, out)

    header = out.read_text(encoding="utf-8").splitlines()[0]
    # Ровно этот заголовок ждёт приём на сервере (infra/electronics/kontakt-receive.sh).
    assert header == ",".join(kontakt_collect.COLUMNS)
    catalog = catalog_file.read_catalog_file(out, "kontakt")
    by_id = {product.external_id: product for product in catalog.products}
    # Идентификатор — адрес страницы, как у загрузки 2026-11-13, а не артикул.
    assert set(by_id) == {"televizor-a-1", "televizor-b-2"}
    assert (by_id["televizor-a-1"].price, by_id["televizor-a-1"].discount_price) == (100, 80)
    assert by_id["televizor-a-1"].is_on_sale
    assert by_id["televizor-b-2"].price == 50 and not by_id["televizor-b-2"].is_on_sale
    assert by_id["televizor-b-2"].offer_availability_status == "out_of_stock"
    assert by_id["televizor-a-1"].url == "https://kontakt.az/televizor-a-1"


async def test_too_many_unread_categories_mean_no_file(tmp_path):
    pages = {
        "/_root.data": MENU,
        "/tv.data": kontakt_listing([kontakt_item(sku="A-1")], 1),
        # Сайт насчитал 5, показал 1 — проход неполный оба раза.
        "/phones.data": kontakt_listing([kontakt_item(sku="B-2")], 5),
    }
    scraper, _ = browser(site(pages), tmp_path)
    with pytest.raises(kontakt_collect.CollectError) as error:
        await kontakt_collect.collect(scraper, max_incomplete_share=0.03)
    assert error.value.code == 3
    # С допуском в половину категорий тот же обход файл даёт.
    scraper, _ = browser(site(pages), tmp_path)
    rows = await kontakt_collect.collect(scraper, max_incomplete_share=0.5)
    assert len(rows) == 2


async def test_cloudflare_midway_stops_the_whole_collection(tmp_path):
    pages = {"/_root.data": MENU, "/tv.data": kontakt_listing([kontakt_item()], 1)}

    def answer(url):
        if urlsplit(url).path == "/phones.data":
            return {"status": 403, "text": CLOUDFLARE_CHALLENGE}
        return site(pages)(url)

    scraper, _ = browser(answer, tmp_path)
    with pytest.raises(kontakt_collect.CollectError) as error:
        await kontakt_collect.collect(scraper)
    assert error.value.code == 2


def test_long_category_path_fits_the_database_column():
    path = "/".join(["bolme-" + "x" * 20] * 8)
    label = kontakt_collect.category_label(path)
    assert len(label) <= 100 and path.endswith(label)
    assert kontakt_collect.category_label("tv/oled") == "tv/oled"


def test_file_is_written_atomically(tmp_path):
    out = tmp_path / "sub" / "k.csv"
    kontakt_collect.write_catalog([], out)
    assert out.exists() and not (tmp_path / "sub" / "k.csv.part").exists()
    with out.open(encoding="utf-8") as fh:
        assert next(csv.reader(fh)) == list(kontakt_collect.COLUMNS)
