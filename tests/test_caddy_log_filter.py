"""Журнал Caddy: второй слой — значение параметра-секрета в запись не идёт.

Caddy стоит перед API и фронтендом и пишет в журнал адрес запроса, заголовки
запроса и заголовки ответа. Ссылки из писем токена в них больше не несут
(tests/test_login_link_in_a_browser.py) — это первый слой. Фильтр в блоке `log`
образца `infra/Caddyfile` — второй: он закрывает запрос, который несёт
`?token=…` сам (письмо прежнего образца, скрипт), и ключ API в заголовке.
Таких блоков в образце два: `log` сайта (запись о каждом запросе) и общий
`log default` — туда Caddy пишет вторую запись о запросе, который не смог
обслужить (сервис за ним не ответил, 502), с тем же адресом и заголовками.

Здесь два уровня проверки:

- по тексту образца, всегда: какие поля чистятся и что делает шаблон с адресом.
  Шаблон исполняет Python, а не Caddy, — для этого шаблона они читают его
  одинаково, но это не тот же движок;
- настоящим Caddy, если он есть (`PM_TEST_CADDY=<путь>` или `caddy` в PATH):
  образец целиком, с подменёнными адресом сайта и портами, принимает запросы, и
  значения ищутся в том, что он записал. Контрольный запуск — тот же образец с
  `format console` — обязан их записать. **В CI Caddy нет, там этот уровень
  пропускается**: руками он пройден 2026-10-12 на Caddy 2.6.2 и 2.10.2.

Чего здесь нет: живого файла `/etc/caddy/Caddyfile`. Его ставит root руками, он
от образца отличается, и версия Caddy на сервере из репозитория не видна —
порядок установки и проверки на сервере в docs/RUNBOOK.md.
"""

from __future__ import annotations

import http.client
import http.server
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "infra" / "Caddyfile"

_REGEXP_LINE = re.compile(r"^\s*(\S+) regexp `([^`]+)` (\S+)\s*$", re.MULTILINE)
_DELETE_LINE = re.compile(r"^\s*(\S+) delete\s*$", re.MULTILINE)


def _log_block() -> str:
    """Блок `log` сайта: запись о каждом запросе."""
    sample = SAMPLE.read_text(encoding="utf-8")
    (block,) = re.findall(r"^    log \{\n.*?^    \}\n", sample, re.MULTILINE | re.DOTALL)
    return block


def _default_log_block() -> str:
    """Общий `log default`: туда Caddy пишет о запросе, который не смог обслужить."""
    sample = SAMPLE.read_text(encoding="utf-8")
    (block,) = re.findall(r"^    log default \{\n.*?^    \}\n", sample, re.MULTILINE | re.DOTALL)
    return block


def _hide(address: str) -> str:
    """Что шаблон из образца делает с адресом."""
    ((_field, pattern, replacement),) = [
        found for found in _REGEXP_LINE.findall(_log_block()) if found[0] == "request>uri"
    ]
    assert replacement == "${1}<hidden>"
    return re.sub(pattern, r"\g<1><hidden>", address)


@pytest.mark.parametrize("block_of", [_log_block, _default_log_block])
def test_the_filter_covers_every_place_an_address_is_written(block_of):
    block = block_of()
    cleaned = {field: pattern for field, pattern, _ in _REGEXP_LINE.findall(block)}

    # Адрес запроса, адрес страницы, с которой он пришёл, и адрес редиректа.
    assert set(cleaned) == {"request>uri", "request>headers>Referer", "resp_headers>Location"}
    assert len(set(cleaned.values())) == 1, "шаблон у трёх полей должен быть один"
    assert set(_DELETE_LINE.findall(block)) == {
        "request>headers>Next-Router-State-Tree",
        "request>headers>X-Api-Key",
    }
    assert "format filter {" in block


def test_both_journals_are_filtered_alike():
    """Запись о запросе и запись об ошибке запроса несут одни и те же поля."""
    fields = [
        sorted(
            _REGEXP_LINE.findall(block) + [(f, "delete", "") for f in _DELETE_LINE.findall(block)]
        )
        for block in (_log_block(), _default_log_block())
    ]

    assert fields[0] == fields[1]
    # Вид записей прежний: у сайта `console`, у общего журнала под systemd — `json`.
    assert "wrap console" in _log_block() and "output stderr" in _log_block()
    assert "wrap json" in _default_log_block()


def test_the_names_are_those_the_sentry_layer_calls_credentials():
    """Один список на оба слоя: имя, добавленное в `observability`, нужно и здесь."""
    from src import observability

    (pattern,) = {pattern for _, pattern, _ in _REGEXP_LINE.findall(_log_block())}
    for name in observability._CREDENTIAL_NAME_RE.pattern.split("|"):
        assert name in pattern, name


@pytest.mark.parametrize(
    ("address", "written"),
    [
        ("/auth/verify?token=SECRET", "/auth/verify?token=<hidden>"),
        ("/az/set-password?token=SECRET", "/az/set-password?token=<hidden>"),
        ("/x?utm=mail&token=SECRET&next=2", "/x?utm=mail&token=<hidden>&next=2"),
        ("/x?access_token=SECRET", "/x?access_token=<hidden>"),
        ("/x?Token=SECRET", "/x?Token=<hidden>"),
        ("/x?api_key=SECRET&q=1", "/x?api_key=<hidden>&q=1"),
        ("/x?apikey=SECRET", "/x?apikey=<hidden>"),
        ("/x?key=SECRET&q=1", "/x?key=<hidden>&q=1"),
        ("/x?q=1&code=SECRET", "/x?q=1&code=<hidden>"),
        ("/x?session_id=SECRET", "/x?session_id=<hidden>"),
        ("/x?new_password=SECRET", "/x?new_password=<hidden>"),
        ("/x?token=", "/x?token=<hidden>"),
        ("/x?a=1&token=ONE&b=2&code=TWO", "/x?a=1&token=<hidden>&b=2&code=<hidden>"),
    ],
)
def test_a_secret_parameter_loses_its_value(address, written):
    assert _hide(address) == written


@pytest.mark.parametrize(
    "address",
    [
        "/health",
        "/api/v1/dash/comparison?search=kreon&min_spread=5",
        "/api/v1/dash/alerts?type=price_drop_pct&page=2",
        # `key` и `code` — только имя целиком: это не они.
        "/x?keyword=kreon&barcode=4600&category_code=7&postcode=1000",
        # Слово в пути или в значении — не имя параметра.
        "/auth/link",
        "/x?q=token",
        "/token/x?q=1",
    ],
)
def test_other_parameters_stay_readable(address):
    assert _hide(address) == address


# ─── Настоящий Caddy ─────────────────────────────────────────────────────────

CANARIES = {
    "link_token": "TOK1-canary-a1",
    "password_link_token": "TOK2-canary-b2",
    "referer_token": "TOK3-canary-c3",
    "router_state_token": "TOK4-canary-d4",
    "api_key": "APIKEY5-canary-e5",
    "second_place_token": "TOK6-canary-f6",
    # Запрос, на который сервис за Caddy не ответил: об этом Caddy пишет ещё раз.
    "unserved_token": "TOK7-canary-g7",
    "unserved_referer_token": "TOK8-canary-h8",
    "unserved_api_key": "APIKEY9-canary-i9",
}
KEPT = "kreon-KEPT"


def _caddy() -> str:
    found = os.environ.get("PM_TEST_CADDY") or shutil.which("caddy")
    if not found:
        pytest.skip("нужен Caddy: PM_TEST_CADDY=<путь> или `caddy` в PATH (в CI его нет)")
    return found


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _Upstream(http.server.BaseHTTPRequestHandler):
    """Вместо API и фронтенда: `/set-password` уводит на `/az/…`, как Next."""

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        if path == "/set-password":
            self.send_response(307)
            self.send_header("Location", f"/az{path}?{query}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args) -> None:
        pass


def _sample_for_a_test(port: int, upstream: int, *, filtered: bool) -> str:
    sample = SAMPLE.read_text(encoding="utf-8")
    # Без сертификатов: сайт слушает http на своём порту.
    sample, switched = re.subn(r"(?m)^    admin off\b", "    auto_https off\n    admin off", sample)
    assert switched == 1
    assert "leaddrive.cloud, www.leaddrive.cloud {" in sample
    sample = sample.replace("leaddrive.cloud, www.leaddrive.cloud {", f"http://127.0.0.1:{port} {{")
    # Grafana в образце — третий сервис за Caddy. Здесь он «лежит»: порт свободен,
    # и запрос к нему Caddy обслужить не может (502).
    dead = _free_port()
    sample, grafana = re.subn(
        r"reverse_proxy 127\.0\.0\.1:3001\b", f"reverse_proxy 127.0.0.1:{dead}", sample
    )
    sample, proxies = re.subn(
        r"reverse_proxy 127\.0\.0\.1:(?:8080|3000)\b", f"reverse_proxy 127.0.0.1:{upstream}", sample
    )
    assert (grafana, proxies) == (1, 2)
    if not filtered:
        plain = (
            "    log {\n        output stderr\n        format console\n        level INFO\n    }\n"
        )
        sample = sample.replace(_log_block(), plain).replace(_default_log_block(), "")
        assert "format filter" not in sample
    return sample


def _get(port: int, target: str, **headers: str) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def _journal_of(caddy: str, tmp_path: Path, upstream: int, *, filtered: bool) -> str:
    port = _free_port()
    config = tmp_path / ("filtered.Caddyfile" if filtered else "plain.Caddyfile")
    config.write_text(_sample_for_a_test(port, upstream, filtered=filtered), encoding="utf-8")
    environment = {**os.environ, "XDG_DATA_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)}
    arguments = ["--config", str(config), "--adapter", "caddyfile"]
    checked = subprocess.run(
        [caddy, "validate", *arguments], capture_output=True, text=True, env=environment
    )
    assert checked.returncode == 0, f"caddy validate отказал:\n{checked.stderr[-2000:]}"

    journal = tmp_path / f"{config.stem}.log"
    with journal.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [caddy, "run", *arguments], stdout=subprocess.DEVNULL, stderr=output, env=environment
        )
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                _get(port, "/health")
                break
            except OSError:
                assert process.poll() is None, journal.read_text(encoding="utf-8")[-2000:]
                assert time.monotonic() < deadline, "Caddy не начал отвечать"
                time.sleep(0.05)
        c = CANARIES
        assert _get(port, f"/auth/verify?token={c['link_token']}") == 200
        assert _get(port, f"/set-password?token={c['password_link_token']}") == 307
        assert (
            _get(
                port,
                "/az/set-password",
                **{
                    "Referer": f"https://leaddrive.cloud/az/set-password?token={c['referer_token']}",
                    "Next-Router-State-Tree": f"%5B%22%2Faz%2Fset-password%23token%3D{c['router_state_token']}%22%5D",
                },
            )
            == 200
        )
        assert (
            _get(
                port,
                f"/api/v1/dash/comparison?search={KEPT}&min_spread=5",
                **{"X-API-Key": c["api_key"]},
            )
            == 200
        )
        assert _get(port, f"/x?utm=mail&access_token={c['second_place_token']}&next=2") == 200
        assert (
            _get(
                port,
                f"/grafana/x?token={c['unserved_token']}",
                **{
                    "Referer": f"https://leaddrive.cloud/p?token={c['unserved_referer_token']}",
                    "X-API-Key": c["unserved_api_key"],
                },
            )
            == 502
        )
        time.sleep(0.3)
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    return journal.read_text(encoding="utf-8", errors="replace")


_LOCATION = re.compile(r'"Location":\s*\["([^"]*)"\]')


def _records_holding(journal: str, value: str) -> list[str]:
    return [line for line in journal.splitlines() if value in line]


@pytest.fixture(scope="module")
def journals(tmp_path_factory) -> dict[str, str]:
    caddy = _caddy()
    out = tmp_path_factory.mktemp("caddy_log_filter")
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    try:
        port = upstream.server_address[1]
        return {
            "filtered": _journal_of(caddy, out, port, filtered=True),
            "plain": _journal_of(caddy, out, port, filtered=False),
        }
    finally:
        upstream.shutdown()
        upstream.server_close()


def test_without_the_filter_caddy_writes_every_value(journals):
    """Контроль: так пишет Caddy сейчас — адрес, Referer, Location, заголовки."""
    missing = [name for name, value in CANARIES.items() if value not in journals["plain"]]
    assert not missing, f"проба не видит в журнале без фильтра: {missing}"
    # Токен ссылки на создание пароля лежит и в адресе редиректа.
    assert _LOCATION.findall(journals["plain"]) == [
        f"/az/set-password?token={CANARIES['password_link_token']}"
    ]
    # О запросе без ответа Caddy пишет дважды: запись о запросе и запись об ошибке.
    assert len(_records_holding(journals["plain"], CANARIES["unserved_token"])) == 2
    assert "http.log.error" in journals["plain"]


def test_with_the_filter_caddy_writes_none_of_them(journals):
    journal = journals["filtered"]

    leaked = [name for name, value in CANARIES.items() if value in journal]
    assert not leaked, f"в журнале Caddy: {leaked}"
    # Записи на месте и прежнего вида, параметры без секрета читаются.
    assert journal.count("handled request") == journals["plain"].count("handled request")
    assert '"uri": "/auth/verify?token=<hidden>"' in journal
    assert _LOCATION.findall(journal) == ["/az/set-password?token=<hidden>"]
    # Обе записи о запросе без ответа на месте — и обе без значения.
    assert len(_records_holding(journal, "/grafana/x?token=<hidden>")) == 2
    assert "http.log.error" in journal
    assert f"search={KEPT}&min_spread=5" in journal
    assert "/x?utm=mail&access_token=<hidden>&next=2" in journal
