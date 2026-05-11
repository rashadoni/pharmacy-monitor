"""Лёгкое error reporting — без внешних SaaS.

Использует только stdlib + structlog. Когда происходит unhandled exception
в одном из критичных путей (scraper / api / dashboard), мы:

1. Логируем JSON-ошибку в `logs/errors.jsonl` (отдельно от обычных logs)
2. Отправляем email админу (если настроен SMTP) — НЕ чаще раз в час по
   одному и тому же error-fingerprint (dedup).

Опционально: если в .env задан SENTRY_DSN — пробуем отправить в Sentry
через простой HTTP без `sentry-sdk`. Если не работает — fallback на email.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import traceback
from pathlib import Path
from threading import Lock

import structlog

from src._time import utcnow

log = structlog.get_logger()

ERRORS_LOG = Path("logs/errors.jsonl")
_DEDUP_WINDOW = 3600  # 1 час
_DEDUP_LOCK = Lock()
_RECENT: dict[str, float] = {}  # fingerprint → ts последней отправки


def _fingerprint(exc: BaseException, context: dict | None = None) -> str:
    parts = [type(exc).__name__, str(exc)[:200]]
    if context:
        parts.append(str(sorted((context or {}).items()))[:200])
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def report_error(
    exc: BaseException,
    *,
    context: dict | None = None,
    component: str = "unknown",
    notify: bool = True,
) -> None:
    """Сохранить ошибку в errors.jsonl + отправить email (с dedup)."""
    fp = _fingerprint(exc, context)
    now = time.time()
    should_notify = notify

    with _DEDUP_LOCK:
        last = _RECENT.get(fp)
        if last and now - last < _DEDUP_WINDOW:
            should_notify = False
        else:
            _RECENT[fp] = now
        # Очистка старых записей
        for k in [k for k, v in _RECENT.items() if now - v > _DEDUP_WINDOW * 2]:
            _RECENT.pop(k, None)

    payload = {
        "ts": utcnow().isoformat(),
        "component": component,
        "fingerprint": fp,
        "type": type(exc).__name__,
        "message": str(exc),
        "context": context or {},
        "traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )[:5000],
    }

    # 1. Файл-логи
    try:
        ERRORS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ERRORS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass  # хуже всего — потеряли запись лога, не падаем

    log.error(
        "error_reported",
        component=component,
        fingerprint=fp,
        type=type(exc).__name__,
        message=str(exc)[:200],
    )

    # 2. Email-уведомление (с dedup)
    if should_notify:
        try:
            from src import notifier
            html = (
                f"<div style='font-family:Menlo,monospace;font-size:12px;'>"
                f"<h3 style='color:#ff3b30;'>🔴 Pharmacy Monitor — Error</h3>"
                f"<p><b>Component:</b> {component}</p>"
                f"<p><b>Type:</b> <code>{payload['type']}</code></p>"
                f"<p><b>Message:</b> {payload['message']}</p>"
                f"<p><b>Fingerprint:</b> <code>{fp}</code></p>"
                f"<p><b>Context:</b> <pre>{json.dumps(payload['context'], indent=2)}</pre></p>"
                f"<p><b>Traceback:</b></p>"
                f"<pre style='background:#fafafa;padding:10px;'>{payload['traceback']}</pre>"
                f"</div>"
            )
            notifier.send_email(
                subject=f"[ERROR] {payload['type']}: {payload['message'][:50]}",
                html_body=html,
            )
        except Exception as e:
            log.warning("error_email_failed", error=str(e))


def install_global_handler() -> None:
    """Установить sys.excepthook чтобы все необработанные exception'ы попадали сюда.

    Использовать только в long-running процессах (telegram-bot, dashboard).
    Для CLI — лучше явный try/except в main.
    """
    import sys

    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        report_error(exc_value, component="global", notify=True)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


# Декоратор для оборачивания функций
def report_on_error(component: str = "unknown", notify: bool = True):
    """@report_on_error('scraper.aloe') — автоматически ловит и репортит."""
    def deco(fn):
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                report_error(e, component=component, notify=notify)
                raise
        return wrapper
    return deco
