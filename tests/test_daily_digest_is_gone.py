"""Ежедневного дайджеста получателя нет — и вернуться незаметно он не должен.

До 2026-10-13 у пользователя была галочка «Ежедневный дайджест»
(`tenant_users.daily_digest`). Стоящая галочка отменяла ему письма сразу после
сбора («событие попадёт в дайджест»), а дайджеста по таймеру не слал никто:
суточный юнит зовёт прежний `pharmacy-monitor digest`, который ходит по таблице
`recipients` и галочку не читает. Получатель с галочкой оставался без писем
вовсе. На сервере это спало — галочка была снята у всех, флаг
`DAILY_DIGEST_ENABLED` стоял 0 и не давал её поставить через интерфейс.

Владелец выбрал убрать дайджест целиком, а не достраивать: колонка остаётся в
базе (она NOT NULL, удаление — миграцией и отдельным релизом), но по ней никто
ничего не решает. Половина возврата — снова читать колонку, не заведя таймер, —
и есть прежняя поломка, поэтому здесь растяжки на каждое место, где она жила.

Чего тесты не видят: колонку, прочитанную сырым SQL или именем, собранным из
частей; юнит, положенный на сервер руками мимо `infra/systemd/`.
"""

from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy.orm import sessionmaker

from src import storage, tenants
from src._time import utcnow
from src.main import cli

REPO = Path(__file__).resolve().parents[1]
COLUMN = "daily_digest"
FLAG = "DAILY_DIGEST_ENABLED"
WEEKLY_UNIT = REPO / "infra/systemd/pharmacy-monitor-digest-weekly.service"


def _lines_naming_the_column(source: str) -> list[int]:
    """Строки, где код называет колонку: атрибут, именованный аргумент, поле
    класса, строка-имя (`getattr(user, "daily_digest")`, ключ словаря).
    Комментарии и описания, где слово стоит внутри текста, не считаются."""
    lines = set()
    for node in ast.walk(ast.parse(source)):
        named = (
            (isinstance(node, ast.Attribute) and node.attr == COLUMN)
            or (isinstance(node, ast.keyword) and node.arg == COLUMN)
            or (isinstance(node, ast.Name) and node.id == COLUMN)
            or (isinstance(node, ast.Constant) and node.value == COLUMN)
        )
        if named:
            # У именованного аргумента своей строки до Python 3.9 не было.
            lines.add(getattr(node, "lineno", None) or node.value.lineno)
    return sorted(lines)


def test_nothing_in_src_reads_or_writes_the_column():
    uses = [
        f"{path.relative_to(REPO)}:{line}"
        for path in sorted((REPO / "src").rglob("*.py"))
        for line in _lines_naming_the_column(path.read_text())
    ]

    # Одно место — сама колонка в модели.
    assert len(uses) == 1, uses
    assert uses[0].startswith("src/storage.py:")


def test_the_probe_sees_every_way_of_naming_the_column():
    """Контроль: растяжка выше не зелёная оттого, что ничего не умеет найти."""
    sample = (
        "def f(user, session, U):\n"
        "    if user.daily_digest:\n"  # 2: атрибут
        "        return U(daily_digest=True)\n"  # 3: именованный аргумент
        "    return getattr(user, 'daily_digest')\n"  # 4: строка-имя
        "class Prefs:\n"
        "    daily_digest: bool = True\n"  # 6: поле модели запроса
        "# daily_digest в комментарии и в тексте ниже не считается\n"
        "NOTE = 'колонка tenant_users.daily_digest без читателей'\n"
    )

    assert _lines_naming_the_column(sample) == [2, 3, 4, 6]


def test_nothing_reads_the_daily_switch():
    """Флаг в env сервера остался (`DAILY_DIGEST_ENABLED=0`), читателя у него нет:
    поставленный в 1, он ничего не включает."""
    readers = []
    for path in sorted((REPO / "src").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Constant) and node.value == FLAG:
                readers.append(f"{path.relative_to(REPO)}:{node.lineno}")

    assert readers == []
    assert FLAG not in (REPO / ".env.example").read_text()


def test_dashboard_has_no_daily_digest_setting():
    """Ни галочки, ни её подписей: страница, типы запросов, тексты трёх языков."""
    frontend = REPO / "frontend"
    files = [
        path
        for folder in ("src", "messages", "e2e")
        for path in sorted((frontend / folder).rglob("*"))
        if path.is_file() and path.suffix in {".ts", ".tsx", ".json"}
    ]
    assert len(files) > 50, "каталоги фронтенда не найдены — проверять нечего"

    named = [
        str(path.relative_to(REPO))
        for path in files
        if COLUMN in path.read_text() or "digests.daily" in path.read_text()
    ]

    assert named == []


def test_no_unit_or_workflow_sends_a_daily_per_user_digest():
    places = [*sorted((REPO / "infra").rglob("*")), *sorted((REPO / ".github").rglob("*"))]
    texts = {path: path.read_text(errors="ignore") for path in places if path.is_file()}
    assert WEEKLY_UNIT in texts

    assert [
        str(p.relative_to(REPO)) for p, text in texts.items() if "notify digest daily" in text
    ] == []


# ─── Команда: недельный остался, ежедневного нет ─────────────────────────────


@pytest.fixture
def subscriber(monkeypatch, db_session):
    engine = db_session.get_bind()
    Session = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda *a, **kw: Session)
    monkeypatch.setattr(storage, "init_db", lambda *a, **kw: None)
    tenant = tenants.get_or_create_default(db_session)
    user = storage.TenantUser(
        tenant_id=tenant.id,
        email="alice@example.com",
        role="admin",
        is_active=True,
        created_at=utcnow(),
        daily_digest=True,
        weekly_digest=True,
    )
    db_session.add(user)
    db_session.add(
        storage.AlertEvent(
            rule_type="site_drop_smoke",
            dedup_key="gone-1",
            severity="warning",
            title="Сайт собран не полностью",
            tenant_id=tenant.id,
            created_at=utcnow(),
        )
    )
    db_session.commit()
    return user


@pytest.fixture
def outbox(monkeypatch):
    from src import notifier

    sent: list[tuple[list[str], str]] = []
    monkeypatch.setattr(
        notifier, "send_email", lambda *, subject, html_body, to: sent.append((to, subject))
    )
    return sent


def test_cli_has_no_daily_digest(subscriber, outbox):
    """Получатель с прежней галочкой есть, событие за сутки есть — раньше
    команда прислала бы ему «дайджест за сутки»."""
    result = CliRunner().invoke(cli, ["notify", "digest", "daily"])

    assert result.exit_code == 2, result.output
    assert "'daily' is not" in result.output
    assert outbox == []


def _unit_command(unit: Path) -> list[str]:
    commands = [
        line.removeprefix("ExecStart=").strip()
        for line in unit.read_text().splitlines()
        if line.startswith("ExecStart=")
    ]
    assert len(commands) == 1, f"ожидался один ExecStart, найдено {commands}"
    binary, *args = shlex.split(commands[0])
    assert binary.endswith("/pharmacy-monitor")
    return args


def test_weekly_unit_command_still_sends_the_weekly_digest(subscriber, outbox):
    """Команда берётся из самого файла юнита: недельный таймер на сервере зовёт
    её каждый понедельник, и правка списка видов не должна была её задеть."""
    args = _unit_command(WEEKLY_UNIT)
    assert args == ["notify", "digest", "weekly"]

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output
    assert "OK: weekly digest sent to 1 recipients" in result.output
    assert outbox == [(["alice@example.com"], "Pharmacy Monitor — дайджест за неделю: 1 событие")]


def test_legacy_daily_unit_is_the_recipients_letter_not_a_per_user_digest():
    """Суточный юнит в репозитории — другой механизм: `pharmacy-monitor digest`
    шлёт по таблице `recipients`. Его эта правка не трогала; тест держит границу,
    чтобы «ежедневный дайджест» в документации не начал значить два разных письма."""
    template = REPO / "infra/systemd/pharmacy-monitor-digest@.service"

    assert _unit_command(template) == ["digest"]
    source = (REPO / "src/digest.py").read_text()
    assert not re.search(r"\bTenantUser\b|tenant_users", source)
