"""Чем кончилась последняя рассылка утреннего письма — файл для сторожа в health.

Письмо с разницей цен клиент ждёт каждое утро, а о несостоявшемся утре никто бы
не узнал: команда письма никого не оповещает, health за юнитами не следит,
журнал владелец не читает. Поэтому каждая настоящая рассылка суточного письма
оставляет здесь запись, а часовой `health-check` сверяет её
(`health._check_daily_letter`) и поднимает тревогу тем же письмом админу, что и
об остальном.

Файл, а не таблица: схему базы ради сторожа не меняем. Пишет его команда письма
(таймер или ручной запуск), читает health — под одним пользователем и из одного
рабочего каталога, с записью в `data/` (юниты в infra/systemd; на это стоит
растяжка в tests/test_daily_letter_watchdog.py). Письмо одному получателю —
`--only` и кнопка «тестовый дайджест» в дашборде — записи не оставляет. Адресов
в файле нет: только время, числа и класс ошибки.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import structlog

log = structlog.get_logger()

STATE_FILE_ENV = "DAILY_LETTER_STATE_FILE"
DEFAULT_STATE_FILE = "data/daily_letter_state.json"

# Сколько часов без рассылки — ещё не тревога. Письмо уходит раз в сутки
# (pharmacy-monitor-digest-daily.timer); два часа сверху — на случайную задержку
# таймера, на саму рассылку и на утро, когда сервер поднялся позже обычного и
# таймер (Persistent=true) догонял пропущенный запуск. Меньше — исправный график
# сам поднимал бы тревогу; больше — о пропущенном утре узнавали бы к вечеру.
# Таймер перестал быть суточным — менять вместе с ним: растяжка в
# tests/test_daily_letter_watchdog.py читает файл таймера.
MAX_AGE_HOURS = 26

REPORT_OK = "ok"
# Отчёт не строился, потому что письмо некому слать: подписчиков нет (или
# `--only` никого не нашёл). Единственный «пропуск», который не проблема.
REPORT_NO_RECIPIENTS = "skipped:no_recipients"
_REPORT_RE = re.compile(r"ok|skipped:[a-z0-9_]+|failed:[A-Za-z_][A-Za-z0-9_]*")
# В файле нет ничего закрытого — время и числа. Читать его должен health под
# своим пользователем, кто бы ни запустил рассылку: временный файл создаётся
# 0600, и запись, сделанную из-под root, health под pm иначе не прочёл бы.
_STATE_FILE_MODE = 0o644


def report_skipped(reason: str) -> str:
    """Отчёта о ценах в письме нет по известной причине (не сбой)."""
    return f"skipped:{reason}"


def report_failed(error_type: str) -> str:
    """Сборка отчёта о ценах упала; `error_type` — класс ошибки, не её текст."""
    return f"failed:{error_type}"


class StateUnreadable(Exception):
    """Файл состояния есть, но понять по нему последнюю рассылку нельзя."""


@dataclass(frozen=True)
class Mailing:
    """Последняя настоящая рассылка суточного письма арендатору."""

    at: datetime  # когда закончилась, UTC без пояса
    recipients: int  # скольким письмо слали; 0 — слать было нечего или некому
    accepted: int  # у скольких почтовый сервер письмо принял
    report: str  # "ok" | "skipped:<причина>" | "failed:<класс ошибки>"

    @property
    def failed_report(self) -> str | None:
        """Класс ошибки, на которой упала сборка отчёта; None — не падала."""
        kind, _, error_type = self.report.partition(":")
        return error_type if kind == "failed" else None

    @property
    def skipped_report(self) -> str | None:
        """Причина, по которой отчёта в письме не было; None — был или упал."""
        kind, _, reason = self.report.partition(":")
        return reason if kind == "skipped" else None


def state_file() -> Path:
    return Path(os.environ.get(STATE_FILE_ENV) or DEFAULT_STATE_FILE)


def record(tenant_id: int, *, at: datetime, recipients: int, accepted: int, report: str) -> bool:
    """Записать исход рассылки. Не бросает: письма к этому моменту уже ушли.

    Запись атомарная — временный файл рядом и `os.replace`: health читает файл
    каждый час и половину записи увидеть не должен. Записи других арендаторов
    сохраняются. Сбой пишется в журнал и возвращает False; сторож тогда через
    `MAX_AGE_HOURS` скажет, что письмо не рассылалось, — это и есть сигнал.
    """
    path = state_file()
    temp_path: Path | None = None
    try:
        try:
            tenants = json.loads(path.read_text(encoding="utf-8"))["tenants"]
            tenants = dict(tenants) if isinstance(tenants, dict) else {}
        except (OSError, ValueError, KeyError, TypeError):
            tenants = {}  # файла нет или он испорчен — начинаем заново
        tenants[str(tenant_id)] = {
            "at": at.isoformat(timespec="seconds"),
            "recipients": recipients,
            "accepted": accepted,
            "report": report,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump({"version": 1, "tenants": tenants}, temp_file, sort_keys=True, indent=1)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_path, _STATE_FILE_MODE)
        os.replace(temp_path, path)
        temp_path = None
        return True
    except Exception as exc:
        log.warning(
            "daily_letter_state_write_failed",
            path=str(path),
            error_type=type(exc).__name__,
            errno=getattr(exc, "errno", None),
        )
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def last_mailing(tenant_id: int) -> Mailing | None:
    """Последняя рассылка арендатору; None — её ещё не было (или файла нет).

    `StateUnreadable` — файл есть, но не читается или записан не нами: молча
    считать это «рассылки не было» нельзя, сторож перестал бы работать.
    """
    path = state_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateUnreadable(type(exc).__name__) from exc
    try:
        tenants = json.loads(text)["tenants"]
        entry = tenants.get(str(tenant_id))
        if entry is None:
            return None
        at = datetime.fromisoformat(entry["at"])
        recipients, accepted, report = entry["recipients"], entry["accepted"], entry["report"]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise StateUnreadable(type(exc).__name__) from exc
    counts_ok = all(type(n) is int and n >= 0 for n in (recipients, accepted))
    if at.tzinfo is not None or not counts_ok or accepted > recipients:
        raise StateUnreadable("ValueError")
    if not isinstance(report, str) or not _REPORT_RE.fullmatch(report):
        raise StateUnreadable("ValueError")
    return Mailing(at=at, recipients=recipients, accepted=accepted, report=report)
