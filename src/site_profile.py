"""Профиль экземпляра: какие сайты он собирает и кто из них клиент.

Один и тот же код обслуживает разных клиентов отдельными экземплярами: своя
база, свои юниты, свой адрес. Чем экземпляры различаются, записано здесь —
набор сайтов, сайт клиента, подписи, ритм сбора. Профиль выбирает переменная
окружения ``PM_PROFILE``; без неё экземпляр аптечный, и для аптечного прода
этот модуль не меняет ничего: все списки ниже у профиля ``pharmacy`` дословно
те, что раньше были прошиты в полутора десятках модулей.

Модуль без зависимостей от остального кода: списки сайтов читают и
``storage``, и ``cadence``, и всё, что стоит над ними.

Профиль читается один раз, при импорте, — списки сайтов по всему коду
объявлены константами модулей. Сменить профиль у запущенного процесса нельзя;
тесты другого профиля запускают отдельный процесс.

Подробности о втором профиле — ``docs/ELECTRONICS.md``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

PROFILE_ENV = "PM_PROFILE"
DEFAULT_PROFILE = "pharmacy"


@dataclass(frozen=True)
class SiteProfile:
    name: str
    # Сайт клиента — первым: это порядок колонок сравнения и приоритет при
    # равных ценах.
    sites: tuple[str, ...]
    client_site: str
    titles: dict[str, str]
    # Ожидаемый интервал между полными сборами сайта, часы (см. src/cadence.py).
    cadence_hours: dict[str, int]
    # Сайты, которые частичный тик вправе собирать между полными сборами.
    intraday_sites: tuple[str, ...] = ()
    # Сайты, чей каталог приходит файлом, а не сборщиком по расписанию: `run`
    # и `scrape` без `--site` их не собирают.
    imported_sites: tuple[str, ...] = field(default=())

    @property
    def competitor_sites(self) -> tuple[str, ...]:
        return tuple(site for site in self.sites if site != self.client_site)

    def __post_init__(self) -> None:
        if self.client_site not in self.sites:
            raise ValueError(f"profile {self.name}: client site is not among its sites")
        if len(set(self.sites)) != len(self.sites):
            raise ValueError(f"profile {self.name}: a site is listed twice")
        for mapping_name in ("titles", "cadence_hours"):
            if set(getattr(self, mapping_name)) != set(self.sites):
                raise ValueError(
                    f"profile {self.name}: {mapping_name} must cover exactly its sites"
                )
        for listed in (*self.intraday_sites, *self.imported_sites):
            if listed not in self.sites:
                raise ValueError(f"profile {self.name}: {listed} is not among its sites")


PHARMACY = SiteProfile(
    name="pharmacy",
    sites=("pharmonline", "aptekonline", "aloe"),
    client_site="pharmonline",
    titles={"pharmonline": "Pharmonline", "aptekonline": "Aptekonline", "aloe": "Aloe"},
    cadence_hours={"pharmonline": 24, "aptekonline": 24, "aloe": 24},
    intraday_sites=("aloe",),
)

# Kontakt Home сборщиком с сервера не читается (проверка Cloudflare), его
# каталог придёт файлом — отсюда длинный ритм: «устарел» он не через сутки.
# ⚠️ Ритм — это сетка окон, а не возраст файла (`cadence.cadence_window_start`):
# файл, загруженный накануне границы двухнедельного окна, наутро считается
# просроченным. Загрузке из файла (шаг 2в в docs/ELECTRONICS.md) нужно своё
# правило — по возрасту последней загрузки; до тех пор число здесь условное.
ELECTRONICS = SiteProfile(
    name="electronics",
    sites=("kontakt", "irshad", "bakuelectronics"),
    client_site="kontakt",
    titles={
        "kontakt": "Kontakt Home",
        "irshad": "Irshad",
        "bakuelectronics": "Baku Electronics",
    },
    cadence_hours={"kontakt": 24 * 14, "irshad": 24, "bakuelectronics": 24},
    imported_sites=("kontakt",),
)

PROFILES: dict[str, SiteProfile] = {profile.name: profile for profile in (PHARMACY, ELECTRONICS)}


def profile_from_environment(environ: dict[str, str] | None = None) -> SiteProfile:
    """Профиль по ``PM_PROFILE``; пусто — аптечный, незнакомое имя — отказ.

    Отказ, а не аптечный по умолчанию: экземпляр электроники с опечаткой в
    переменной начал бы собирать аптечные сайты в свою базу.
    """
    source = os.environ if environ is None else environ
    name = (source.get(PROFILE_ENV) or "").strip().lower() or DEFAULT_PROFILE
    try:
        return PROFILES[name]
    except KeyError:
        known = ", ".join(sorted(PROFILES))
        raise RuntimeError(
            f"{PROFILE_ENV}={name!r} is not a known profile (known: {known})"
        ) from None


PROFILE = profile_from_environment()

SITES: tuple[str, ...] = PROFILE.sites
CLIENT_SITE: str = PROFILE.client_site
COMPETITOR_SITES: tuple[str, ...] = PROFILE.competitor_sites
SITE_TITLES: dict[str, str] = dict(PROFILE.titles)
INTRADAY_SITES: tuple[str, ...] = PROFILE.intraday_sites
IMPORTED_SITES: tuple[str, ...] = PROFILE.imported_sites


def is_pharmacy() -> bool:
    return PROFILE.name == PHARMACY.name


def viewer_sites(client_site: str | None = None) -> tuple[str, ...]:
    """Сайты экземпляра, «свой» — первым, остальные в порядке профиля.

    `client_site` — чьими глазами смотрит страница. У экземпляра электроники
    им бывает любой из трёх магазинов (показ продукта каждому из них);
    аптечный интерфейс его не меняет. None — клиент профиля.
    """
    client = client_site or CLIENT_SITE
    if client not in SITES:
        raise ValueError(f"unknown client_site: {client!r}")
    return (client, *(site for site in SITES if site != client))
