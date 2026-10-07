"""К боевому серверу ходят только с прошитым host key.

Зачем тест: прод переехал с Hetzner на Contabo (2026-09-03), старый сервер удалён,
а его адрес ещё месяц оставался в исполняемых скриптах. Освобождённый облачный
адрес могут выдать чужой машине, и тогда всё, что берёт ключ у сети
(`ssh-keyscan`, `StrictHostKeyChecking=accept-new`), примет её за прод и отправит
туда команды и данные. Семь workflow из двадцати делали именно так.

Покрываем:
- каждый workflow, который ходит на прод, пишет в known_hosts тот же ключ, что
  лежит в `infra/prod_known_hosts`
- скрипты, которые ходят на прод, требуют прошитый ключ
- в исполняемых каталогах никто не доверяет ключу, полученному из сети
- адрес удалённого сервера не возвращается в исполняемые каталоги

Чего тест не видит: обход, записанный иначе, чем перечислено в `TRUSTS_NETWORK`,
и доступ к проду по имени, а не по адресу. Это сторож от возврата известных
ошибок, а не доказательство, что непрошитого доступа нет. Запреты срабатывают и
на прозу: в `.github/`, `infra/`, `scripts/` эти слова не писать даже в
комментарии.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROD_IP = "13.140.186.143"
RETIRED_IPS = ("46.225.149.52",)
# Документы сюда не входят намеренно: в них старый адрес остаётся как история.
EXECUTABLE_DIRS = (".github", "infra", "scripts")
TRUSTS_NETWORK = re.compile(
    r"ssh-keyscan"
    r"|StrictHostKeyChecking[\s=]+[\"']?(accept-new|no|off)\b"
    r"|UserKnownHostsFile[\s=]+[\"']?/dev/null",
    re.IGNORECASE,
)


def _pinned_line() -> str:
    lines = [
        line.strip()
        for line in (ROOT / "infra" / "prod_known_hosts").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert len(lines) == 1, "в infra/prod_known_hosts должен быть ровно один ключ"
    return lines[0]


def _executable_files() -> list[Path]:
    return [
        path
        for name in EXECUTABLE_DIRS
        for path in sorted((ROOT / name).rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    ]


def test_workflows_reaching_prod_pin_the_same_host_key():
    pinned = _pinned_line()
    assert pinned.startswith(f"{PROD_IP} ssh-ed25519 ")

    workflows = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
    reaching = [path for path in workflows if PROD_IP in path.read_text()]
    assert reaching, "ни один workflow не ходит на прод — адрес в тесте устарел?"

    # Ключ должен не просто встречаться в файле, а записываться в known_hosts.
    writes_pinned_key = re.compile(re.escape(f"'{pinned}'") + r" \\\n\s+> ~/\.ssh/known_hosts\n")
    unpinned = [path.name for path in reaching if not writes_pinned_key.search(path.read_text())]
    assert not unpinned, f"host key не прошит или отличается от infra/prod_known_hosts: {unpinned}"


def test_scripts_reaching_prod_require_the_pinned_key():
    scripts = [
        path
        for path in _executable_files()
        if path.suffix == ".sh" and "PROD_HOST" in path.read_text(errors="ignore")
    ]
    assert scripts, "ни один скрипт не ходит на прод — признак PROD_HOST устарел?"

    unpinned = [
        str(path.relative_to(ROOT))
        for path in scripts
        if "prod_known_hosts" not in path.read_text()
        or "StrictHostKeyChecking=yes" not in path.read_text()
    ]
    assert not unpinned, f"скрипт ходит на прод без прошитого host key: {unpinned}"


def test_nothing_takes_a_host_key_from_the_network():
    offenders = [
        str(path.relative_to(ROOT))
        for path in _executable_files()
        if TRUSTS_NETWORK.search(path.read_text(errors="ignore"))
    ]
    assert not offenders, (
        f"ключ сервера берётся у сети, а не из infra/prod_known_hosts: {offenders}"
    )


def test_retired_server_address_stays_out_of_executable_paths():
    offenders = [
        str(path.relative_to(ROOT))
        for path in _executable_files()
        if any(ip in path.read_text(errors="ignore") for ip in RETIRED_IPS)
    ]
    assert not offenders, f"адрес удалённого сервера в исполняемом файле: {offenders}"
