"""Маска на stdout и stderr команды CLI: адрес не выходит и мимо журнала.

Маска журнала (`logging_setup.masking`, `MaskingFormatter`) стоит только на
журнале. `click.echo`, `print`, строка click «Error: …» и трассировка ошибки,
которую никто не поймал, идут в поток напрямую — а вывод команды, запущенной
из GitHub Actions, лежит в открытом журнале шага. Здесь тот же сток закрыт у
самого потока: всё, что пишут в `sys.stdout` и `sys.stderr`, выходит построчно
и после маски, кто бы и чем ни писал.

Последняя линия, а не разрешение печатать адреса. Что идёт мимо неё:

- дочерний процесс и код на C пишут в дескриптор, а не в `sys.stdout`;
- `sys.__stdout__`, `os.write(1, …)` и поток, взятый до установки маски;
- строка, которую разрезал `flush()`: что уже сброшено, обратно не вернуть;
- журнал: у него своя маска, и сюда он не заходит (см. `unmasked`).
"""

from __future__ import annotations

import io
import re
import sys
import threading

from src.logging_setup import mask_addresses

# Вместо слова, в котором после маски остался «@».
WORD_WITHHELD = "<слово скрыто>"

# Адрес с именем в кавычках: `"имя фамилия"@домен`. Пробел внутри кавычек делит
# его на слова, и без этого шаблона первое слово имени осталось бы в выводе.
_QUOTED_NAME = re.compile(r'"[^"\n]{1,64}"@\S*')
_SPACES = re.compile(r"(\s+)")


def mask_line(line: str) -> str:
    """Строка вывода без адресов.

    Маска журнала вырезает то, что узнала. Если после неё в строке остался «@»,
    значит, узнала не всё: имя в кавычках, логин прокси, запись, которой шаблон
    не знает. `logging_setup.without_addresses` в таком случае не показывает
    текст вовсе, но там текст — причина сбоя, без которой вывод обойдётся.
    Здесь строка — это вывод команды или кадр трассировки: выбросить её значит
    потерять и то, где упало. Поэтому скрывается не строка, а слово с «@».
    """
    if "@" not in line:
        return line
    line = mask_addresses(line)
    if "@" not in line:
        return line
    line = _QUOTED_NAME.sub(WORD_WITHHELD, line)
    return "".join(WORD_WITHHELD if "@" in word else word for word in _SPACES.split(line))


class _TextSink:
    """Двоичный вход для потока, у которого нет `buffer` (`io.StringIO`)."""

    def __init__(self, stream) -> None:
        self._stream = stream

    @property
    def closed(self) -> bool:
        return bool(getattr(self._stream, "closed", False))

    def write(self, data: bytes) -> None:
        self._stream.write(data.decode("utf-8", "surrogatepass"))

    def flush(self) -> None:
        self._stream.flush()


class _MaskingWriter(io.BufferedIOBase):
    """Двоичный слой между текстовым потоком и его настоящим буфером.

    Стоит ниже текста, поэтому мимо не проходит ни строка, ни байты: `buffer`
    замаскированного потока — это он и есть. Наружу отдаёт только целые строки:
    `print("a", b)` — это четыре вызова `write`, и адрес может прийти по частям.
    Хвост без перевода строки ждёт его или `flush()`.
    """

    def __init__(self, stream, sink) -> None:
        self._stream = stream
        self._sink = sink
        self._pending: list[bytes] = []
        # Поток, который сбрасывает буфер на каждом переводе строки (stderr,
        # терминал), делает это и под маской — но сбрасываются целые строки, а
        # не хвост: `TextIOWrapper(line_buffering=True)` выпустил бы и его.
        self._flush_each_line = bool(getattr(stream, "line_buffering", False))
        # Поток общий для всех потоков исполнения; RLock — запись может прийти
        # и изнутри записи (обработчик сигнала, `__del__`).
        self._lock = threading.RLock()

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return self._stream.isatty()

    def fileno(self) -> int:
        # Настоящий дескриптор: его наследует дочерний процесс, в него пишет
        # faulthandler. Маски на нём нет.
        return self._stream.fileno()

    @property
    def name(self) -> str:
        return getattr(self._stream, "name", "<masked>")

    def write(self, data) -> int:
        data = bytes(data)
        with self._lock:
            head, newline, tail = data.rpartition(b"\n")
            if not newline:
                self._pending.append(data)
                return len(data)
            lines = b"".join([*self._pending, head, newline])
            self._pending = [tail] if tail else []
            self._emit(lines)
            if self._flush_each_line:
                self._sink.flush()
        return len(data)

    def flush(self) -> None:
        with self._lock:
            if self._pending:
                # Кто зовёт `flush`, тому текст нужен на экране сейчас:
                # приглашение ко вводу, точки прогресса. Адрес, который
                # разрезан этим сбросом, маска уже не узнает.
                tail = b"".join(self._pending)
                self._pending = []
                self._emit(tail)
            self._sink.flush()

    @property
    def closed(self) -> bool:
        # Закрыли поток под маской — закрыта и она: сборщик мусора не станет
        # сбрасывать в него хвост, а запись откажет, как отказала бы без маски.
        return super().closed or bool(getattr(self._sink, "closed", False))

    def close(self) -> None:
        # Сбросить хвост и больше не принимать. Поток под маской не наш:
        # закрывает его тот, кто открыл.
        if not self.closed:
            super().close()

    def _emit(self, data: bytes) -> None:
        if b"@" in data:  # почти весь вывод: не декодируем и не трогаем
            # surrogateescape возвращает любые байты как были, в какой бы
            # кодировке ни писал поток.
            text = data.decode("utf-8", "surrogateescape")
            text = "\n".join(mask_line(line) for line in text.split("\n"))
            data = text.encode("utf-8", "surrogateescape")
        self._sink.write(data)


class MaskedStream(io.TextIOWrapper):
    """`sys.stdout` или `sys.stderr` под маской.

    Настоящий `io.TextIOWrapper` поверх `_MaskingWriter`: кодировка, `isatty`,
    `fileno`, `reconfigure` ведут себя как у потока, который он заменил, и
    click, logging, Playwright не замечают разницы. `unmasked` — тот самый
    поток.
    """

    def __init__(self, stream) -> None:
        buffer = getattr(stream, "buffer", None)
        if buffer is None:
            sink, encoding, errors = _TextSink(stream), "utf-8", "surrogatepass"
        else:
            sink = buffer
            encoding = getattr(stream, "encoding", None) or "utf-8"
            errors = getattr(stream, "errors", None) or "strict"
        super().__init__(
            _MaskingWriter(stream, sink),
            encoding=encoding,
            errors=errors,
            # Текст не задерживается выше маски: порядок строк с журналом,
            # который пишет в `unmasked`, остаётся прежним.
            write_through=True,
        )
        self.unmasked = stream
        self.mode = "w"


def unmasked(stream):
    """Поток, который стоит под маской, — или сам `stream`, если маски нет.

    Журнал пишет сюда: у него своя маска (`logging_setup.masking`), и строку
    журнала она оставляет разборчивой — в JSON-выводе слово, вырезанное
    `mask_line`, унесло бы с собой кавычку и запятую.
    """
    return stream.unmasked if isinstance(stream, MaskedStream) else stream


def mask_output() -> None:
    """Поставить маску на `sys.stdout` и `sys.stderr`. Повторный вызов ничего не меняет.

    Обратно маска не снимается: строку «Error: …» click печатает уже после
    того, как команда закончилась, а трассировку непойманной ошибки —
    интерпретатор, ещё позже. Обе читают `sys.stderr` в момент печати.
    """
    masked: dict[int, MaskedStream] = {}
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None or isinstance(stream, MaskedStream):
            continue
        if id(stream) not in masked:  # CliRunner старого click: это один поток
            stream.flush()
            masked[id(stream)] = MaskedStream(stream)
        setattr(sys, name, masked[id(stream)])


def unmask_output() -> None:
    """Снять маску: команда печатает адрес оператору по назначению.

    Зовёт только `main.OperatorCommand` — класс команд из списка
    `_PRINTS_AN_ADDRESS_BY_DESIGN` в `tests/test_log_carries_no_address.py`.
    Запускать такую команду из workflow тест не даёт.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if isinstance(stream, MaskedStream):
            stream.flush()
            setattr(sys, name, stream.unmasked)
