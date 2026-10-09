"""Маска на stdout и stderr команды CLI — `src/output_mask.py`.

Четвёртый слой правила «адрес получателя не попадает в журнал» (первые три — в
`tests/test_log_carries_no_address.py`). Маска журнала стоит только на журнале;
`click.echo`, `print`, строка «Error: …» и трассировка ошибки, которую никто не
поймал, шли в открытый журнал шага Actions как есть. Теперь маска стоит на
самих потоках, и держит это уже не правило «печатай через
`without_addresses`», а сток.

Порядок тестов — по граням, на которых такая обёртка обычно ломается:

1. что делается со строкой (`mask_line`);
2. поток: адрес по частям, `flush`, байты и `buffer`, кодировка, закрытие;
3. установка: дважды, один поток на оба имени, снятие;
4. настоящая группа `cli` под `CliRunner`: вывод команды, «Error: …», ошибка
   разбора опций, журнал рядом с выводом, команды «по назначению»;
5. настоящий интерпретатор: трассировка непойманной ошибки, свой
   `sys.excepthook`, хвост при выходе, дочерний процесс;
6. кто вправе снять маску — чтение кода.
"""

from __future__ import annotations

import _thread
import ast
import gc
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import click
import pytest
import structlog
from click.testing import CliRunner
from sqlalchemy.orm import sessionmaker

from src import main, output_mask, storage, watchlist
from src.output_mask import WORD_WITHHELD, MaskedStream, mask_line
from tests.test_log_carries_no_address import (
    _PRINTS_AN_ADDRESS_BY_DESIGN,
    _RUN_FROM_A_WORKFLOW,
    ADDRESS,
)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
# Запись адреса, которую маска журнала не узнаёт вовсе.
QUOTED = '"viewer name"@client.example'
# Так адрес приходит на самом деле: в тексте ошибки базы, среди параметров запроса.
DB_ERROR = (
    "(psycopg.errors.UniqueViolation) duplicate key value violates unique constraint "
    f"[parameters: {{'email': '{ADDRESS}', 'name': '{QUOTED}'}}]"
)

_HOW_TO_FIX_THE_OPERATOR_COMMANDS = (
    "Команды, с которых снята маска вывода (`cls=OperatorCommand`), разошлись со "
    "списком `_PRINTS_AN_ADDRESS_BY_DESIGN` в tests/test_log_carries_no_address.py. "
    "Без маски, но не в списке: {unlisted}. В списке, но под маской: {masked}. "
    "Команда из списка печатает адрес оператору, и под маской вместо него выйдет "
    "`<address>`: дай ей `cls=OperatorCommand`. Команде не из списка этот класс "
    "ставить нельзя — её вывод может оказаться в открытом журнале шага Actions; "
    "внести команду в список — решение владельца, а не правка теста."
)
_HOW_TO_FIX_A_WAY_PAST_THE_MASK = (
    "В `src/` появился или исчез путь мимо маски вывода. Лишнее: {extra}. "
    "Пропало: {missing}. Маску снимает только `OperatorCommand.invoke`, а поток "
    "под маской берёт только журнал (`_setup_logging`, `_LogStream`): у него своя "
    "маска. Остальному коду `unmask_output`, `unmasked`, `sys.__stdout__` / "
    "`sys.__stderr__`, `os.write` и присваивание `sys.stdout` / `sys.stderr` не "
    "нужны: печатай через `click.echo` или `print`. «При импорте» значит, что "
    "`sys.stdout` / `sys.stderr` взят на уровне модуля, в теле класса или "
    "значением по умолчанию: маску ставит запуск команды, позже, и такой поток "
    "остаётся без неё. Бери поток в момент печати, внутри функции. Если строка "
    "переехала в другую функцию — поправь список в тесте."
)
_HOW_TO_FIX_A_CHILD_PROCESS = (
    "Список файлов `src/`, которые запускают дочерний процесс, разошёлся с кодом: "
    "{found}. Ребёнок пишет в "
    "дескриптор, который унаследовал, — мимо `sys.stdout` и мимо маски вывода. "
    "Забери его вывод себе (`capture_output=True`) и напечатай сам — тогда он "
    "пройдёт через маску, — и внеси файл в `_STARTS_A_CHILD_PROCESS`."
)


# ─── 1. Что делается со строкой ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("line", "shown"),
    [
        ("run_finished products=9424 status=ok", "run_finished products=9424 status=ok"),
        ("", ""),
        # Что маска журнала узнаёт, то и вырезает: остальное в строке на месте.
        (f"OK: {ADDRESS} (—) is_active=True", "OK: <address> (—) is_active=True"),
        (
            f"{{'{ADDRESS}': (550, b'5.1.1 <{ADDRESS}>')}}",
            "{'<address>': (550, b'5.1.1 <<address>>')}",
        ),
        ("почта@пример.рф не принята", "<address> не принята"),
        # Чего она не узнала, не показывается словом: в слове остался «@».
        (f"отказано {QUOTED} сегодня", f"отказано {WORD_WITHHELD} сегодня"),
        ("ssh pm@13.140.186.143 упал", f"ssh {WORD_WITHHELD} упал"),
        ("proxy=http://***@gate.example.com:7000 refused", f"{WORD_WITHHELD} refused"),
        ("admin@local", WORD_WITHHELD),
        ("    @cli.command('run')", f"    {WORD_WITHHELD}"),
        # Оба случая в одной строке — и строка остаётся строкой.
        (DB_ERROR, DB_ERROR.replace(ADDRESS, "<address>").replace(f"{QUOTED}'}}]", WORD_WITHHELD)),
    ],
)
def test_a_line_leaves_without_addresses_and_stays_a_line(line, shown):
    assert mask_line(line) == shown
    assert "@" not in mask_line(line)


def test_a_line_with_an_address_is_never_dropped_whole():
    """`without_addresses` не показала бы текст вовсе; у строки вывода скрыто слово."""
    frame = f'  File "src/main.py", line 4167, in init_db_cmd  # {QUOTED}'
    assert mask_line(frame) == f'  File "src/main.py", line 4167, in init_db_cmd  # {WORD_WITHHELD}'


def test_the_start_of_a_name_the_log_mask_leaves_is_left_here_too():
    """Предел, а не обещание: маска журнала считает апостроф краем адреса, и
    «@» после неё в слове уже нет — второму правилу не за что зацепиться."""
    assert mask_line("отказано o'brien@client.example") == "отказано o'<address>"


def test_masking_a_line_stays_linear():
    """Маска стоит на пути каждой строки вывода: длинное слово без пробелов и
    тысячи кавычек не должны разбираться квадратично."""
    line = "payload=" + "a" * 50_000 + ' "' * 50_000 + f" {QUOTED} pm@host"
    started = time.perf_counter()
    masked = mask_line(line)
    assert time.perf_counter() - started < 5
    assert masked.endswith(f" {WORD_WITHHELD} {WORD_WITHHELD}") and "@" not in masked


# ─── 2. Поток ────────────────────────────────────────────────────────────────


def _stream(encoding: str = "utf-8", **kwargs) -> io.TextIOWrapper:
    """Поток, какой маска получает на деле: текст поверх двоичного буфера."""
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, **kwargs)


def _reached(stream: io.TextIOWrapper) -> str:
    """Что дошло до буфера потока. Без `flush`: сколько маска держит у себя — предмет проверки."""
    return stream.buffer.getvalue().decode("utf-8")


def test_an_address_written_in_pieces_is_still_recognised():
    """`print("a", b)` — четыре вызова `write`; чужой код пишет и по букве."""
    under = _stream()
    masked = MaskedStream(under)
    for piece in ("refused vie", "wer@cli", "ent.example", " twice\n"):
        masked.write(piece)
    print("user", ADDRESS, "id", 7, file=masked)
    assert _reached(under) == "refused <address> twice\nuser <address> id 7\n"


def test_a_line_is_held_until_it_ends_and_flush_lets_the_tail_out():
    under = _stream()
    masked = MaskedStream(under)
    masked.write("строка целиком\nПароль:")
    assert _reached(under) == "строка целиком\n"
    # Кто зовёт `flush`, тому текст нужен сейчас: приглашение ко вводу.
    masked.flush()
    assert _reached(under) == "строка целиком\nПароль:"


def test_a_progress_bar_is_drawn_as_it_goes():
    """Полоса прогресса перерисовывает одну строку через `\\r` и сбрасывает
    буфер: до перевода строки дело доходит только в конце."""
    under = _stream()
    masked = MaskedStream(under)
    drawn = []
    for done in ("\r[#   ] 25%", "\r[##  ] 50%"):
        masked.write(done)
        masked.flush()
        drawn.append(_reached(under))
    assert drawn == ["\r[#   ] 25%", "\r[#   ] 25%\r[##  ] 50%"]


def test_flush_cuts_an_address_and_the_mask_cannot_put_it_back():
    """Предел, а не обещание. Что сброшено, уже у читателя; вторая половина
    выходит скрытой, первая — как была. В `src/` так не пишет никто."""
    under = _stream()
    masked = MaskedStream(under)
    masked.write("vie")
    masked.flush()
    masked.write("wer@client.example\n")
    assert _reached(under) == "vie<address>\n"


def test_a_stream_that_flushes_each_line_does_not_flush_the_tail(monkeypatch):
    """stderr сбрасывает буфер на каждом переводе строки. Под маской тоже — но
    хвост после перевода строки остаётся ждать своего конца."""
    under = _stream(line_buffering=True)
    flushed = []
    monkeypatch.setattr(under.buffer, "flush", lambda: flushed.append(_reached(under)))
    masked = MaskedStream(under)
    masked.write("первая\nvie")
    assert flushed == ["первая\n"]
    masked.write("wer@client.example\n")
    assert _reached(under) == "первая\n<address>\n"


def test_a_stream_that_flushes_each_line_draws_a_progress_bar_without_flush():
    """Такой поток сбрасывает буфер и на возврате каретки: полосу прогресса в
    stderr рисуют без `flush`."""
    under = _stream(line_buffering=True)
    masked = MaskedStream(under)
    masked.write("\r[#   ] 25%")
    assert _reached(under) == "\r[#   ] 25%"

    # Поток без такого сброса ждёт — как ждал бы и без маски.
    plain = _stream()
    held = MaskedStream(plain)
    held.write("\r[#   ] 25%")
    assert _reached(plain) == ""


def test_a_carriage_return_before_a_newline_is_not_a_progress_bar():
    """`\\r\\n` — конец строки, а не кадр полосы: хвост после него ждёт."""
    under = _stream(line_buffering=True)
    masked = MaskedStream(under)
    masked.write("HTTP/1.1 200 OK\r\nX-To: viewer")
    masked.write("@client.example\r\n")
    assert _reached(under) == "HTTP/1.1 200 OK\r\nX-To: <address>\r\n"


def test_a_progress_bar_does_not_release_another_threads_tail():
    under = _stream(line_buffering=True)
    masked = MaskedStream(under)
    half_written, go_on = threading.Event(), threading.Event()

    def slow():
        masked.write("vie")
        half_written.set()
        go_on.wait(5)
        masked.write("wer@client.example\n")

    writer = threading.Thread(target=slow, daemon=True)
    writer.start()
    assert half_written.wait(5)
    masked.write("\r[#   ] 25%")
    assert _reached(under) == "\r[#   ] 25%"
    go_on.set()
    writer.join(5)
    assert _reached(under) == "\r[#   ] 25%<address>\n"


def test_flush_reaches_the_file_under_the_stream(tmp_path):
    """У `BytesIO` сбрасывать нечего; у настоящего файла между потоком и
    диском ещё один буфер, и `click.echo` ждёт, что `flush` опустошит и его."""
    path = tmp_path / "out.txt"
    with open(path, "w", encoding="utf-8") as real:
        masked = MaskedStream(real)
        masked.write("started\n")
        assert path.read_text(encoding="utf-8") == ""
        masked.flush()
        assert path.read_text(encoding="utf-8") == "started\n"


def test_lines_from_two_threads_are_not_glued_together():
    """`print("a", b)` — несколько вызовов `write`, и между ними успевает
    вклиниться другой поток исполнения. Хвост у каждого свой."""
    under = _stream()
    masked = MaskedStream(under)
    half_written, go_on = threading.Event(), threading.Event()

    def slow():
        masked.write("медленный ")
        half_written.set()
        go_on.wait(5)
        masked.write(f"{ADDRESS}\n")

    writer = threading.Thread(target=slow, daemon=True)
    writer.start()
    assert half_written.wait(5)
    print("быстрый", ADDRESS, file=masked)
    # Чужой хвост `flush` не трогает: его хозяин ещё пишет.
    masked.flush()
    assert _reached(under) == "быстрый <address>\n"
    go_on.set()
    writer.join(5)
    assert _reached(under) == "быстрый <address>\nмедленный <address>\n"


def test_the_tail_of_a_finished_thread_is_not_lost():
    """Дописать строку больше некому: её выпускает первый же `flush` — при
    выходе из процесса его зовёт интерпретатор."""
    under = _stream()
    masked = MaskedStream(under)
    writer = threading.Thread(target=masked.write, args=("хвост потока",))
    writer.start()
    writer.join()
    assert _reached(under) == ""
    masked.flush()
    assert _reached(under) == "хвост потока"


def test_a_thread_python_did_not_start_does_not_keep_its_tail_forever():
    """Поток исполнения, заведённый из кода на C, о своём конце не сообщает:
    ждать, что он допишет строку, нельзя — хвост выпускает первый же `flush`."""
    under = _stream()
    masked = MaskedStream(under)
    written = threading.Event()

    def foreign():
        masked.write("хвост чужого потока")
        written.set()

    _thread.start_new_thread(foreign, ())
    assert written.wait(5)
    masked.flush()
    assert _reached(under) == "хвост чужого потока"


def test_closing_the_mask_lets_every_tail_out_and_leaves_the_stream_open():
    under = _stream()
    masked = MaskedStream(under)
    half_written, go_on = threading.Event(), threading.Event()

    def slow():
        masked.write("хвост живого потока")
        half_written.set()
        go_on.wait(5)

    writer = threading.Thread(target=slow, daemon=True)
    writer.start()
    assert half_written.wait(5)
    masked.close()
    go_on.set()
    writer.join(5)
    assert masked.closed and not under.closed
    assert _reached(under) == "хвост живого потока"


def test_a_close_that_fails_still_closes_the_mask():
    """Канал оборвался, и хвост выпустить не удалось. Маска всё равно закрыта:
    иначе сборщик мусора пришёл бы закрывать её ещё раз."""

    class Sink:
        def write(self, data: bytes) -> None:
            pass

        def flush(self) -> None:
            raise BrokenPipeError

    writer = output_mask._MaskingWriter(io.StringIO(), Sink())
    with pytest.raises(BrokenPipeError):
        writer.close()
    assert writer.closed


def test_a_flush_from_inside_a_flush_finds_the_tails_already_taken():
    """Запись в поток под маской зовёт `flush` ещё раз — так делает обработчик
    сигнала с `click.echo`. Хвосты к этому времени забрал вложенный вызов."""

    class Sink:
        def __init__(self) -> None:
            self.written: list[bytes] = []
            self.writer = None

        def write(self, data: bytes) -> None:
            self.written.append(data)
            if len(self.written) == 1:
                self.writer.flush()

        def flush(self) -> None:
            pass

    sink = Sink()
    sink.writer = output_mask._MaskingWriter(io.StringIO(), sink)
    for tail in (b"first", b"second"):
        finished = threading.Thread(target=sink.writer.write, args=(tail,))
        finished.start()
        finished.join()
    failed = []

    def flush():
        try:
            sink.writer.flush()
        except Exception as error:
            failed.append(error)

    flushing = threading.Thread(target=flush, daemon=True)
    flushing.start()
    flushing.join(timeout=5)
    assert not flushing.is_alive(), "сброс изнутри сброса повис на замке"
    assert failed == []
    assert sorted(sink.written) == [b"first", b"second"]


def test_a_flush_waits_for_a_write_in_progress():
    """Запись в медленный канал держит замок: сброс из другого потока
    исполнения не вклинивается в её середину."""
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Sink:
        def write(self, data: bytes) -> None:
            calls.append("запись началась")
            entered.set()
            release.wait(5)
            calls.append("запись кончилась")

        def flush(self) -> None:
            calls.append("сброс")

    writer = output_mask._MaskingWriter(io.StringIO(), Sink())
    writing = threading.Thread(target=writer.write, args=(b"line\n",), daemon=True)
    writing.start()
    assert entered.wait(5)
    flushing = threading.Thread(target=writer.flush, daemon=True)
    flushing.start()
    flushing.join(0.3)
    assert flushing.is_alive(), "сброс не дождался записи"
    release.set()
    writing.join(5)
    flushing.join(5)
    assert calls == ["запись началась", "запись кончилась", "сброс"]


def test_bytes_written_to_the_buffer_are_masked_too():
    """`sys.stdout.buffer` — обычный способ напечатать мимо текстового слоя."""
    under = _stream()
    masked = MaskedStream(under)
    masked.buffer.write(memoryview(f"bytes {ADDRESS} ".encode()))
    masked.buffer.write("почта@пример.рф\n".encode())
    click.echo(f"echo {ADDRESS}".encode(), file=masked)
    assert _reached(under) == "bytes <address> <address>\necho <address>\n"


def test_bytes_the_mask_cannot_read_pass_unchanged():
    """Маска правит только слово с адресом: чужая кодировка и буква, разрезанная
    между двумя вызовами, доходят байт в байт."""
    under = _stream()
    masked = MaskedStream(under)
    cp1251 = "отказ ".encode("cp1251")
    half = "я".encode()
    masked.buffer.write(cp1251 + half[:1])
    masked.buffer.write(half[1:] + f" {ADDRESS} \xff\n".encode("latin-1"))
    assert under.buffer.getvalue() == cp1251 + half + b" <address> \xff\n"


def test_click_goes_around_an_ascii_stream_and_still_meets_the_mask(monkeypatch):
    """Поток с кодировкой ASCII click считает сломанным и пишет байты прямо в
    его `buffer`. Будь там настоящий буфер, маска осталась бы в стороне."""
    under = _stream(encoding="ascii")
    monkeypatch.setattr(sys, "stdout", under)
    monkeypatch.setattr(sys, "stderr", under)
    output_mask.mask_output()
    click.echo(f"кириллица {ADDRESS}")
    click.echo(f"ошибка {QUOTED}", err=True)
    sys.stdout.flush()
    assert _reached(under) == f"кириллица <address>\nошибка {WORD_WITHHELD}\n"


def test_the_masked_stream_answers_for_the_stream_it_hides():
    """Кодировку и терминал читает click, дескриптор — Playwright: под маской
    они видят то же, что без неё."""
    under = _stream(encoding="latin-1", errors="backslashreplace")
    masked = MaskedStream(under)
    assert (masked.encoding, masked.errors) == ("latin-1", "backslashreplace")
    assert masked.isatty() is False and masked.writable() and not masked.closed

    class Terminal(io.TextIOWrapper):
        def isatty(self) -> bool:
            return True

    assert MaskedStream(Terminal(io.BytesIO(), encoding="utf-8")).isatty() is True
    assert masked.unmasked is under
    # Потока без дескриптора Playwright ждёт именно с этой ошибкой.
    with pytest.raises(io.UnsupportedOperation):
        masked.fileno()
    with open(os.devnull, "w", encoding="utf-8") as real:
        masked = MaskedStream(real)
        assert (masked.fileno(), masked.name, masked.mode) == (real.fileno(), real.name, "w")


def test_a_stream_without_a_buffer_is_masked_as_well():
    """`contextlib.redirect_stdout(io.StringIO())` — поток без двоичного слоя."""
    under = io.StringIO()
    masked = MaskedStream(under)
    print(f"в память {ADDRESS} \udcff", file=masked)
    masked.flush()
    assert under.getvalue() == "в память <address> \udcff\n"
    under.close()
    assert masked.closed


def test_dropping_the_mask_neither_closes_the_stream_nor_complains(monkeypatch):
    """`CliRunner` и pytest возвращают свой поток на место, и маска уходит в
    мусор. Закрой она поток под собой — следующий тест остался бы без вывода."""
    unraisable = []
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)

    under = _stream()
    masked = MaskedStream(under)
    masked.write("хвост")
    del masked
    gc.collect()
    assert not under.closed and not under.buffer.closed
    assert _reached(under) == "хвост"

    # Поток под маской закрыли раньше неё: закрыта и она, как был бы закрыт он
    # сам. Сбрасывать хвост некуда, и сборщик мусора не пытается (с Python 3.13
    # ошибку из `__del__` потока печатают всегда).
    gone = _stream()
    masked = MaskedStream(gone)
    masked.write("хвост")
    gone.close()
    assert masked.closed
    with pytest.raises(ValueError, match="closed file"):
        masked.write("ещё")
    masked.close()
    del masked
    gc.collect()
    assert unraisable == []


def test_a_write_from_inside_a_write_does_not_hang():
    """Обработчик сигнала или `__del__` печатает, пока поток занят записью."""

    class Sink:
        def __init__(self) -> None:
            self.written: list[bytes] = []
            self.writer = None

        def write(self, data: bytes) -> None:
            self.written.append(data)
            if data == b"outer\n":
                self.writer.write(b"nested\n")

        def flush(self) -> None:
            pass

    sink = Sink()
    sink.writer = output_mask._MaskingWriter(io.StringIO(), sink)
    writing = threading.Thread(target=sink.writer.write, args=(b"outer\n",), daemon=True)
    writing.start()
    writing.join(timeout=5)
    assert not writing.is_alive(), "запись изнутри записи повисла на замке"
    assert sink.written == [b"outer\n", b"nested\n"]


# ─── 3. Установка ────────────────────────────────────────────────────────────


def test_the_mask_goes_on_both_streams_once(monkeypatch):
    out, err = _stream(), _stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    output_mask.mask_output()
    first = (sys.stdout, sys.stderr)
    output_mask.mask_output()

    assert (sys.stdout, sys.stderr) == first
    assert (sys.stdout.unmasked, sys.stderr.unmasked) == (out, err)
    assert output_mask.unmasked(sys.stdout) is out and output_mask.unmasked(out) is out


def test_a_process_without_streams_is_left_alone(monkeypatch):
    """У демона с закрытыми дескрипторами `sys.stdout` — `None`."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    output_mask.mask_output()
    output_mask.unmask_output()
    assert sys.stdout is None and sys.stderr is None


def test_one_stream_under_both_names_gets_one_mask(monkeypatch):
    """`CliRunner` до click 8.2 ставит один поток и на stdout, и на stderr: две
    маски держали бы два хвоста и путали порядок строк."""
    both = _stream()
    monkeypatch.setattr(sys, "stdout", both)
    monkeypatch.setattr(sys, "stderr", both)
    output_mask.mask_output()
    assert sys.stdout is sys.stderr
    print("out", ADDRESS)
    print("err", ADDRESS, file=sys.stderr)
    assert _reached(both) == "out <address>\nerr <address>\n"


def test_what_was_printed_before_the_mask_stays_ahead_of_it(monkeypatch):
    out = _stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", _stream())
    out.write("до маски\n")
    output_mask.mask_output()
    print("после")
    assert _reached(out) == "до маски\nпосле\n"


def test_taking_the_mask_off_lets_the_tail_out_first(monkeypatch):
    out, err = _stream(), _stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    output_mask.mask_output()
    sys.stdout.write(f"под маской {ADDRESS}")
    # Маску кто-то держит (обработчик журнала сторонней библиотеки): сборщик
    # мусора хвост за неё не сбросит. И она остаётся рабочей.
    held = sys.stdout

    output_mask.unmask_output()

    assert (sys.stdout, sys.stderr) == (out, err) and held.unmasked is out
    assert not held.closed
    print(f" и без неё {ADDRESS}")
    out.flush()
    assert _reached(out) == f"под маской <address> и без неё {ADDRESS}\n"


def test_taking_the_mask_off_does_not_lose_the_tail_of_a_thread_still_writing(monkeypatch):
    """Свою строку он допишет уже в поток без маски: начало лежит в ней."""
    out = _stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", _stream())
    output_mask.mask_output()
    half_written, go_on = threading.Event(), threading.Event()

    def slow():
        sys.stdout.write("начало под маской, ")
        half_written.set()
        go_on.wait(5)
        sys.stdout.write("конец без неё\n")

    writer = threading.Thread(target=slow, daemon=True)
    writer.start()
    assert half_written.wait(5)
    # Маску кто-то держит: сборщик мусора хвост за неё не выпустит.
    held = sys.stdout
    output_mask.unmask_output()
    go_on.set()
    writer.join(5)
    out.flush()
    assert _reached(out) == "начало под маской, конец без неё\n"
    assert held.unmasked is out


# ─── 4. Настоящая группа `cli` ───────────────────────────────────────────────


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    """`cli` пишет в `logs/` текущего каталога и меняет общий конфиг журнала."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PHARMACY_LOG_JSON", raising=False)
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    yield
    for handler in root.handlers:
        handler.close()
    root.handlers[:], root.level = saved[0], saved[1]
    structlog.reset_defaults()


@pytest.fixture
def probe(cli_env):
    """Временная команда под настоящей группой `cli`.

    Маску ставит колбэк группы, а не команда: что верно для этой, верно для
    любой, которую напишут завтра.
    """

    def register(body, cls: type[click.Command] = click.Command) -> None:
        main.cli.add_command(cls("zz-probe", callback=body))

    yield register
    main.cli.commands.pop("zz-probe", None)


def test_whatever_a_command_prints_past_the_log_leaves_without_addresses(probe):
    def body():
        click.echo(f"OK: отправлено {ADDRESS}")
        click.echo(f"предупреждение для {QUOTED}", err=True)
        print("получатели:", [ADDRESS, "second@client.example"])
        sys.stderr.write(f"не доставлено {ADDRESS}\n")

    probe(body)
    result = CliRunner().invoke(main.cli, ["zz-probe"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "OK: отправлено <address>\nполучатели: ['<address>', '<address>']\n"
    assert result.stderr == f"предупреждение для {WORD_WITHHELD}\nне доставлено <address>\n"


def test_the_error_line_click_prints_after_the_command_is_masked(probe):
    """`raise click.ClickException(str(e))` — так делают `scrape`, `ai-crawl`,
    `tenant add`, `seed-demo`. Строку click печатает, когда команда уже
    закончилась: маска к этому времени должна стоять."""

    def body():
        try:
            raise RuntimeError(DB_ERROR)
        except RuntimeError as e:
            raise click.ClickException(str(e)) from e

    probe(body)
    result = CliRunner().invoke(main.cli, ["zz-probe"])

    assert result.exit_code == 1
    assert result.stderr.startswith("Error: (psycopg.errors.UniqueViolation) duplicate key")
    assert "'email': '<address>'" in result.stderr and WORD_WITHHELD in result.stderr
    assert "@" not in result.output, result.output


def test_a_real_command_that_prints_the_error_as_is_is_masked(cli_env, db_session, monkeypatch):
    """`tenant add` печатает текст пойманной ошибки без `without_addresses`."""
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: factory)

    def refuse(*args, **kwargs):
        raise ValueError(DB_ERROR)

    monkeypatch.setattr("src.tenants.create_tenant", refuse)
    result = CliRunner().invoke(main.cli, ["tenant", "add", "acme", "Acme"])

    assert result.exit_code == 1
    assert "Error: (psycopg.errors.UniqueViolation)" in result.output
    assert "@" not in result.output, result.output


def test_an_option_error_of_a_command_is_masked(cli_env):
    """Опции команды click разбирает уже после колбэка группы."""
    result = CliRunner().invoke(main.cli, ["run", "--limit", ADDRESS])
    assert result.exit_code == 2
    assert "Invalid value for '--limit': '<address>'" in result.output, result.output


@pytest.mark.parametrize("as_json", [False, True])
def test_the_log_goes_around_the_output_mask(probe, monkeypatch, as_json):
    """У журнала своя маска, и строку она оставляет разборчивой: логин прокси
    и имя хоста в ней не адрес. Маска вывода вырезала бы слово целиком, а в
    JSON-выводе — вместе с кавычкой и запятой."""
    if as_json:
        monkeypatch.setenv("PHARMACY_LOG_JSON", "1")
    proxy = "http://***@gate.example.com:7000"

    def body():
        structlog.get_logger().info("scrape_using_proxy", proxy=proxy, user=ADDRESS)
        logging.getLogger("some.library").warning("proxy %s for %s", proxy, ADDRESS)
        click.echo(f"proxy {proxy} for {ADDRESS}")

    probe(body)
    result = CliRunner().invoke(main.cli, ["zz-probe"])

    assert result.exit_code == 0, result.output
    ours, library, echoed = result.stdout.splitlines()
    assert proxy in ours and "<address>" in ours
    if as_json:
        assert json.loads(ours)["proxy"] == proxy
    assert library == f"proxy {proxy} for <address>"
    assert echoed == f"proxy {WORD_WITHHELD} for <address>"
    assert ADDRESS not in result.output


def _recipients(db_session, monkeypatch) -> None:
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: factory)
    watchlist.add_recipient(db_session, ADDRESS, "Иван")


def test_a_command_that_prints_addresses_by_design_prints_them(cli_env, db_session, monkeypatch):
    """`recipient list` с `<address>` вместо адресов оператору не нужна."""
    _recipients(db_session, monkeypatch)
    result = CliRunner().invoke(main.cli, ["recipient", "list"])
    assert result.exit_code == 0, result.output
    assert f"[✓] {ADDRESS}  Иван" in result.output


def test_the_same_listing_from_any_other_command_is_masked(probe, db_session, monkeypatch):
    _recipients(db_session, monkeypatch)
    probe(lambda: main.recipient_list.callback(active_only=False))
    result = CliRunner().invoke(main.cli, ["zz-probe"])
    assert result.exit_code == 0, result.output
    assert "[✓] <address>  Иван" in result.output and "@" not in result.output


def test_only_the_operator_command_class_takes_the_mask_off(probe):
    """Снимает маску класс команды, а не вызов её функции: `run`, позвавший
    функцию команды «по назначению», остаётся под маской."""
    shown = {}

    def body():
        shown["stdout"] = type(sys.stdout)
        click.echo(ADDRESS)

    probe(body)
    assert CliRunner().invoke(main.cli, ["zz-probe"]).output == "<address>\n"
    assert shown["stdout"] is MaskedStream

    main.cli.commands.pop("zz-probe")
    probe(body, cls=main.OperatorCommand)
    assert CliRunner().invoke(main.cli, ["zz-probe"]).output == f"{ADDRESS}\n"
    assert shown["stdout"] is not MaskedStream


def _commands(group: click.Group, path: tuple[str, ...] = ()):
    for name, command in group.commands.items():
        if isinstance(command, click.Group):
            yield from _commands(command, (*path, name))
        else:
            yield " ".join((*path, name)), command


def test_no_help_text_is_eaten_by_the_mask(cli_env):
    """Описание команды печатается уже под маской. Имя юнита `…@daily.timer` в
    нём она приняла бы за адрес — и оператор прочёл бы `<address>`."""
    eaten = []
    for name, _ in _commands(main.cli):
        result = CliRunner().invoke(main.cli, [*name.split(), "--help"])
        assert result.exit_code == 0, result.output
        if "<address>" in result.output or WORD_WITHHELD in result.output:
            eaten.append(name)
    assert eaten == [], (
        f"В описании команд {eaten} есть слово с «@»: в `--help` вместо него выйдет "
        "`<address>` или `<слово скрыто>`. Перефразируй описание без «@»."
    )


def test_the_mask_is_off_exactly_for_the_commands_listed_as_printing_an_address():
    unmasked = {
        name for name, command in _commands(main.cli) if isinstance(command, main.OperatorCommand)
    }
    listed = set(_PRINTS_AN_ADDRESS_BY_DESIGN.values())
    assert unmasked == listed, _HOW_TO_FIX_THE_OPERATOR_COMMANDS.format(
        unlisted=sorted(unmasked - listed) or "нет", masked=sorted(listed - unmasked) or "нет"
    )


def test_no_command_run_from_a_workflow_has_the_mask_off():
    """То же, что следует из двух списков, — но сказанное прямо и по дереву click."""
    commands = dict(_commands(main.cli))
    unmasked = {
        name for name in _RUN_FROM_A_WORKFLOW if isinstance(commands[name], main.OperatorCommand)
    }
    assert unmasked == set(), _HOW_TO_FIX_THE_OPERATOR_COMMANDS.format(
        unlisted=sorted(unmasked), masked="нет"
    )


# ─── 5. Настоящий интерпретатор ──────────────────────────────────────────────

_ERROR_NOBODY_CATCHES = f"""
def refuse():
    raise RuntimeError({DB_ERROR!r})
storage.init_db = refuse
"""
_OWN_EXCEPTHOOK = (
    _ERROR_NOBODY_CATCHES
    + """
from src import error_reporting
error_reporting.report_error = lambda *args, **kwargs: None
error_reporting.install_global_handler()
"""
)


def _run_cli(tmp_path, setup: str, *argv: str, **environ: str) -> subprocess.CompletedProcess:
    """Запустить CLI отдельным процессом: то, что печатает сам интерпретатор,
    `CliRunner` не покажет — он ловит ошибку раньше."""
    script = f"import sys\nimport click\nfrom src import main, storage\n{setup}\nmain.cli({list(argv)!r})\n"
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    env.pop("PHARMACY_LOG_JSON", None)
    env.update(environ)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


@pytest.mark.parametrize("setup", [_ERROR_NOBODY_CATCHES, _OWN_EXCEPTHOOK], ids=["default", "own"])
def test_the_traceback_of_an_error_nobody_caught_is_masked(tmp_path, setup):
    """`storage.init_db()` стоит в командах вне `try`. Трассировку печатает
    интерпретатор, когда click уже вышел, — и свой `sys.excepthook`
    (`error_reporting.install_global_handler`) зовёт для этого штатный."""
    done = _run_cli(tmp_path, setup, "init-db")

    assert done.returncode == 1, done.stderr
    assert "Traceback (most recent call last)" in done.stderr
    assert "in init_db_cmd" in done.stderr
    assert "RuntimeError: (psycopg.errors.UniqueViolation)" in done.stderr
    assert "'email': '<address>'" in done.stderr and WORD_WITHHELD in done.stderr
    assert "@" not in done.stdout + done.stderr, done.stderr


def test_a_background_thread_stuck_on_a_write_does_not_hang_the_exit():
    """Фоновый поток пишет в канал, который никто не читает, и держит замок
    маски; основной поток закончил. Python без маски ждёт секунду и обрывает
    процесс — под маской он не должен ждать вечно: зависший сбор держит замок
    сбора, и следующий не начнётся."""
    script = """
import os, sys, threading, time
from src import output_mask
read_end, write_end = os.pipe()
sys.stdout = open(write_end, "w")
output_mask.mask_output()
threading.Thread(target=print, args=("x" * 1_000_000,), daemon=True).start()
time.sleep(0.5)
"""
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        timeout=20,
    )
    # Проверка — что процесс вообще закончился: иначе `subprocess.run` бросил бы
    # `TimeoutExpired`. Чем он закончился, решает Python — тем же обрывом, что
    # и без маски.
    assert done.returncode is not None


def test_the_tail_without_a_newline_is_written_when_the_process_ends(tmp_path):
    setup = f"""
@main.cli.command("zz-probe")
def probe():
    print("хвост без перевода строки {ADDRESS}", end="")
"""
    done = _run_cli(tmp_path, setup, "zz-probe")
    assert done.returncode == 0, done.stderr
    assert done.stdout == "хвост без перевода строки <address>"


def test_the_log_goes_around_the_mask_in_a_real_process_too(tmp_path):
    """Под `CliRunner` этого не видно: `structlog.PrintLogger`, получив
    настоящий stdout, печатает в `sys.stdout` как тот есть на момент печати — то
    есть обратно через маску вывода. Настоящий stdout есть только у процесса."""
    proxy = "http://***@gate.example.com:7000"
    setup = f"""
import structlog
@main.cli.command("zz-probe")
def probe():
    structlog.get_logger().info("scrape_using_proxy", proxy={proxy!r}, user={ADDRESS!r})
    click.echo("proxy {proxy}")
"""
    done = _run_cli(tmp_path, setup, "zz-probe", PHARMACY_LOG_JSON="1")
    assert done.returncode == 0, done.stderr
    logged, echoed = done.stdout.splitlines()
    assert json.loads(logged) | {"timestamp": ""} == {
        "event": "scrape_using_proxy",
        "proxy": proxy,
        "user": "<address>",
        "level": "info",
        "timestamp": "",
    }
    assert echoed == f"proxy {WORD_WITHHELD}"


def test_a_child_process_writes_past_the_mask(tmp_path):
    """Предел, а не обещание: ребёнок наследует дескриптор и пишет в него сам.
    Через маску идёт только то, что родитель забрал и напечатал."""
    setup = f"""
import subprocess
@main.cli.command("zz-probe")
def probe():
    child = [sys.executable, "-c", "print('child {ADDRESS}')"]
    subprocess.run(child, check=True)
    click.echo(subprocess.run(child, check=True, capture_output=True, text=True).stdout, nl=False)
"""
    done = _run_cli(tmp_path, setup, "zz-probe")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [f"child {ADDRESS}", "child <address>"]


# ─── 6. Кто вправе снять маску — чтение кода ─────────────────────────────────
#
# Растяжки, а не ограждение: сверяют имена и ловят случайное, не намеренное.
# `import os as o; o.write(1, …)`, `open(1, "w", closefd=False)`,
# `contextlib.redirect_stdout` они не видят.

# Файл, функция, имя. Маску снимает одно место; поток под ней берёт журнал.
_WAYS_PAST_THE_MASK = {
    ("src/main.py", "OperatorCommand.invoke", "unmask_output"),
    ("src/main.py", "_setup_logging", "unmasked"),
    ("src/main.py", "_LogStream.msg", "unmasked"),
}
_NAMES_PAST_THE_MASK = {"unmask_output", "__stdout__", "__stderr__"}
# `unmasked` — слово обычное: считается только атрибутом и в импорте.
_ATTRIBUTES_PAST_THE_MASK = _NAMES_PAST_THE_MASK | {"unmasked"}
_CALLS_PAST_THE_MASK = {"os.write"}
_STREAM_NAMES = {"sys.stdout", "sys.stderr"}
# Кто в `src/` запускает дочерний процесс через `subprocess`. Оба забирают его
# вывод себе: `observability` читает ответ git, `dashboard` — журнал сбора для
# страницы. Чего проверка не видит: Playwright запускает свой драйвер сам, из
# своего кода, и stderr драйвера идёт в дескриптор мимо маски.
_STARTS_A_CHILD_PROCESS = {"src/observability.py", "src/dashboard.py"}
_CHILD_PROCESS_MODULES = {"subprocess", "multiprocessing", "pty"}
_OS_STARTS_A_CHILD = ("system", "popen", "fork", "exec", "spawn", "posix_spawn", "startfile")
_STARTS_A_CHILD = ("create_subprocess", "subprocess_exec", "subprocess_shell")


def _scoped(node: ast.AST, owner: str = "", called: bool = False):
    """Каждый узел дерева, имя того, внутри чего он стоит («Класс.метод»), и
    исполняется ли он при вызове функции — а не при импорте модуля."""
    for child in ast.iter_child_nodes(node):
        yield child, owner, called
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            inner = owner if isinstance(child, ast.Lambda) else f"{owner}.{child.name}".lstrip(".")
            # Значения по умолчанию, аннотации и декораторы вычисляются там,
            # где функция объявлена, а не там, где её зовут.
            declared = [child.args, *getattr(child, "decorator_list", [])]
            declared += filter(None, [getattr(child, "returns", None)])
            for part in declared:
                yield part, owner, called
                yield from _scoped(part, owner, called)
            for part in child.body if isinstance(child.body, list) else [child.body]:
                yield part, inner, True
                yield from _scoped(part, inner, True)
        elif isinstance(child, ast.ClassDef):
            yield from _scoped(child, f"{owner}.{child.name}".lstrip("."), called)
        else:
            yield from _scoped(child, owner, called)


def ways_past_the_mask(sources: dict[str, str]) -> set[tuple[str, str, str]]:
    """Где код обходит маску вывода: снимает её, берёт поток под ней, пишет в
    дескриптор, ставит на место `sys.stdout` свой поток — или берёт `sys.stdout`
    при импорте модуля, когда маски на нём ещё нет."""
    found = set()
    for path, source in sources.items():
        for node, owner, called in _scoped(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                found |= {
                    (path, owner, alias.name)
                    for alias in node.names
                    if alias.name in _ATTRIBUTES_PAST_THE_MASK
                }
                if node.module == "sys" and not called:
                    found |= {
                        (path, owner, f"sys.{alias.name} при импорте")
                        for alias in node.names
                        if f"sys.{alias.name}" in _STREAM_NAMES
                    }
            if isinstance(node, ast.Name) and node.id in _NAMES_PAST_THE_MASK:
                found.add((path, owner, node.id))
            if isinstance(node, ast.Attribute):
                if node.attr in _ATTRIBUTES_PAST_THE_MASK:
                    found.add((path, owner, node.attr))
                elif ast.unparse(node) in _CALLS_PAST_THE_MASK:
                    found.add((path, owner, ast.unparse(node)))
                elif ast.unparse(node) in _STREAM_NAMES and not called:
                    found.add((path, owner, f"{ast.unparse(node)} при импорте"))
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign | ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                for part in ast.walk(target):
                    if ast.unparse(part) in _STREAM_NAMES:
                        found.add((path, owner, f"{ast.unparse(part)} ="))
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "setattr" and node.args:
                if ast.unparse(node.args[0]) == "sys":
                    found.add((path, owner, "setattr(sys, …)"))
    return found


def child_processes(sources: dict[str, str]) -> set[str]:
    """Файлы, которые запускают дочерний процесс, — по именам модулей и вызовов."""
    found = set()
    for path, source in sources.items():
        for node in ast.walk(ast.parse(source)):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
                if node.module == "os":
                    names += [
                        "subprocess"
                        for alias in node.names
                        if alias.name.startswith(_OS_STARTS_A_CHILD)
                    ]
                names += [
                    "subprocess"
                    for alias in node.names
                    if alias.name.startswith(_STARTS_A_CHILD) or alias.name == "ProcessPoolExecutor"
                ]
            elif isinstance(node, ast.Name):
                names = ["subprocess"] if node.id == "ProcessPoolExecutor" else []
            elif isinstance(node, ast.Attribute):
                from_os = ast.unparse(node.value) == "os" and node.attr.startswith(
                    _OS_STARTS_A_CHILD
                )
                starts = node.attr.startswith(_STARTS_A_CHILD)
                if from_os or starts or node.attr == "ProcessPoolExecutor":
                    names = ["subprocess"]
            if set(names) & _CHILD_PROCESS_MODULES:
                found.add(path)
    return found


def _src_sources() -> dict[str, str]:
    return {
        str(path.relative_to(ROOT)): path.read_text(encoding="utf-8")
        for path in sorted(SRC.rglob("*.py"))
        if path.name != "output_mask.py"
    }


def test_only_the_operator_command_and_the_log_reach_past_the_mask():
    found = ways_past_the_mask(_src_sources())
    assert found == _WAYS_PAST_THE_MASK, _HOW_TO_FIX_A_WAY_PAST_THE_MASK.format(
        extra=sorted(found - _WAYS_PAST_THE_MASK) or "нет",
        missing=sorted(_WAYS_PAST_THE_MASK - found) or "нет",
    )


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("def run_cmd():\n    output_mask.unmask_output()\n", {("run_cmd", "unmask_output")}),
        # Под чужим именем вызов не виден — виден сам импорт.
        (
            "from src.output_mask import unmask_output as off\ndef run_cmd():\n    off()\n",
            {("", "unmask_output")},
        ),
        ("from src.output_mask import unmasked\n", {("", "unmasked")}),
        ("def f():\n    output_mask.unmasked(sys.stderr).write(x)\n", {("f", "unmasked")}),
        ("def f():\n    sys.stdout.unmasked.write(x)\n", {("f", "unmasked")}),
        ("def f():\n    print(x, file=sys.__stdout__)\n", {("f", "__stdout__")}),
        ("from sys import __stderr__\n__stderr__.write(x)\n", {("", "__stderr__")}),
        ("class A:\n    def f(self):\n        os.write(1, data)\n", {("A.f", "os.write")}),
        ("def f():\n    sys.stdout = open(path, 'w')\n", {("f", "sys.stdout =")}),
        (
            "def f():\n    sys.stdout, sys.stderr = a, b\n",
            {("f", "sys.stdout ="), ("f", "sys.stderr =")},
        ),
        ("def f():\n    setattr(sys, name, stream)\n", {("f", "setattr(sys, …)")}),
        # Поток взят при импорте модуля: маску ставит запуск команды, позже.
        ("OUT = sys.stderr\n", {("", "sys.stderr при импорте")}),
        ("handler = logging.StreamHandler(sys.stdout)\n", {("", "sys.stdout при импорте")}),
        ("class A:\n    out = sys.stdout\n", {("A", "sys.stdout при импорте")}),
        (
            "def report(text, out=sys.stdout):\n    out.write(text)\n",
            {("", "sys.stdout при импорте")},
        ),
        (
            "class A:\n    def f(self, *, out=sys.stderr):\n        pass\n",
            {("A", "sys.stderr при импорте")},
        ),
        ("from sys import stderr\n", {("", "sys.stderr при импорте")}),
        (
            "@click.option('--out', default=sys.stdout)\ndef f(out):\n    pass\n",
            {("", "sys.stdout при импорте")},
        ),
        ("def f(out: type(sys.__stdout__)) -> None:\n    pass\n", {("", "__stdout__")}),
        ("def f() -> type(sys.__stderr__):\n    pass\n", {("", "__stderr__")}),
        # Обычная печать и поток, взятый в момент вызова, — не обход.
        ("def f():\n    print(x, file=sys.stderr)\n    sys.stdout.flush()\n", set()),
        ("def f():\n    handler = logging.StreamHandler(sys.stdout)\n", set()),
        ("def f():\n    from sys import stdout\n", set()),
        ("factory = lambda: sys.stdout\n", set()),
        # Обычное слово и запись во временный файл.
        ("def f():\n    unmasked = [row for row in rows if row.shown]\n", set()),
        ("def f():\n    os.fsync(temp_file.fileno())\n    os.fdopen(fd, 'w')\n", set()),
    ],
)
def test_the_code_check_sees_a_way_past_the_mask(source, found):
    assert ways_past_the_mask({"x.py": source}) == {("x.py", *where) for where in found}


def test_only_the_known_places_in_src_start_a_child_process():
    found = child_processes(_src_sources())
    assert found == _STARTS_A_CHILD_PROCESS, _HOW_TO_FIX_A_CHILD_PROCESS.format(
        found=sorted(found ^ _STARTS_A_CHILD_PROCESS)
    )


@pytest.mark.parametrize(
    ("source", "starts"),
    [
        ("import subprocess\n", True),
        ("def f():\n    import subprocess as sp\n", True),
        ("from subprocess import run\n", True),
        ("from multiprocessing.pool import Pool\n", True),
        ("os.system('git status')\n", True),
        ("from os import system\n", True),
        ("await asyncio.create_subprocess_exec('git')\n", True),
        ("from asyncio import create_subprocess_shell\n", True),
        ("await loop.subprocess_exec(protocol, 'git')\n", True),
        ("os.execv(path, argv)\n", True),
        ("os.spawnlp(os.P_WAIT, 'git', 'git')\n", True),
        ("os.posix_spawnp('git', argv, env)\n", True),
        ("os.forkpty()\n", True),
        ("from concurrent.futures import ProcessPoolExecutor\n", True),
        ("pool = concurrent.futures.ProcessPoolExecutor()\n", True),
        ("import os\nos.environ.copy()\n", False),
        ("cursor.execute(statement)\n", False),
        ("from concurrent.futures import ThreadPoolExecutor\n", False),
    ],
)
def test_the_code_check_sees_a_child_process(source, starts):
    assert child_processes({"x.py": source}) == ({"x.py"} if starts else set())
