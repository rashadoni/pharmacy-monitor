"""Приложение для tests/test_access_log_carries_no_query.py — настоящее `src.api.app`.

Грузит его настоящий uvicorn, отдельным процессом и с теми флагами, что стоят в
`infra/systemd/pharmacy-monitor-api.service`:

    python -m uvicorn tests.access_log_probe:app --host 127.0.0.1 --port <порт> --workers 2 …

Отдельным процессом — потому что его stdout и stderr и есть журнал: на сервере
их забирает journald. И именно так, а не `uvicorn.Server` в потоке теста: важен
порядок — uvicorn сначала настраивает журнал, потом импортирует приложение, и
фильтр обязан пережить и то и другое, в каждом из двух рабочих процессов.

От `src.api:app` модуль отличается двумя вещами, обе нужны проверке:

  - выпуск сессии падает для одного пользователя — так запрос по ссылке на вход
    получает ответ 500, а uvicorn печатает трассировку;
  - в режиме `unfiltered` с логгеров uvicorn сняты фильтры `src.access_log`, как
    было до правки. Это контроль: в таком режиме проба обязана увидеть токен в
    журнале, иначе её «чисто» в режиме `shipped` ничего не значит.

Фильтры ставит импорт `src.api` — здесь их никто не ставит.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

MODE = os.environ.get("ACCESS_LOG_PROBE")
if MODE not in ("shipped", "unfiltered"):
    # Импорт подменяет выпуск сессии на весь процесс — в обычном тесте ему не место.
    raise ImportError("tests/access_log_probe.py грузит только uvicorn из своего теста")

FAULT_EMAIL = os.environ["ACCESS_LOG_PROBE_FAULT_EMAIL"]

from src import access_log, api

# venv бывает поставлен на соседний чекаут: проверялся бы чужой код, и молча.
_CHECKOUT = Path(__file__).resolve().parents[1]
assert Path(api.__file__).resolve().parents[1] == _CHECKOUT, f"src взят из {api.__file__}"

_issue_session = api._make_jwt


def _issue_session_or_fail(user_id: int, tenant_id: int, email: str) -> str:
    if email == FAULT_EMAIL:
        raise RuntimeError("probe: session issue failed")
    return _issue_session(user_id, tenant_id, email)


api._make_jwt = _issue_session_or_fail

if MODE == "unfiltered":
    for name in (access_log.ACCESS_LOGGER, access_log.ERROR_LOGGER):
        logger = logging.getLogger(name)
        shipped = [f for f in logger.filters if isinstance(f, access_log.QueryHidden)]
        # Снимать нечего — значит, защита устроена уже не так, и контроль устарел.
        assert len(shipped) == 1, f"{name}: ожидался один фильтр src.access_log, найдено {shipped}"
        logger.removeFilter(shipped[0])

app = api.app
