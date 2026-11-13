"""Список разделов сайта для полного сбора: сверка с сайтом и запись.

Сборщики сайтов без колонки в ``categories`` находят свои разделы сами
(``discover_categories``). Найденное пишет в ``site_routes`` команда
``routes sync`` — отдельно от прогона и по слову человека: прогон читает
готовый список, а не обходит дерево сайта каждую ночь.

Сверка делится на план и запись. План показывает, что изменится, и отказывает,
когда изменение похоже на сбой обхода, а не на перемену на сайте: пустой
список разделов и исчезновение заметной части разделов разом.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src._time import utcnow
from src.storage import SiteRoute

# Больше этой доли действующих разделов разом с сайта не исчезает: скорее обход
# прочитал не всё меню. Такой план применяется только с явным разрешением.
MAX_SHRINK_FRACTION = 0.2


class RouteSyncRefused(RuntimeError):
    """План сверки разделов выглядит как сбой обхода — писать его нельзя."""


@dataclass
class RouteSyncPlan:
    site: str
    discovered: list[str]
    added: list[str] = field(default_factory=list)
    returned: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    active_before: int = 0

    @property
    def changes(self) -> int:
        return len(self.added) + len(self.returned) + len(self.missing)

    def refusal(self) -> str | None:
        """Почему план нельзя писать без явного разрешения; ``None`` — можно."""
        if not self.discovered:
            return "site returned no sections"
        if self.active_before and len(self.missing) > self.active_before * MAX_SHRINK_FRACTION:
            return (
                f"{len(self.missing)} of {self.active_before} active sections are gone "
                "from the site at once"
            )
        return None


def plan_route_sync(session: Session, site: str, discovered: list[str]) -> RouteSyncPlan:
    """Что изменится в списке разделов сайта, если записать найденное на сайте."""
    seen: list[str] = []
    for slug in discovered:
        slug = (slug or "").strip()
        if slug and slug not in seen:
            seen.append(slug)

    known = {
        route.slug: route
        for route in session.scalars(select(SiteRoute).where(SiteRoute.site == site)).all()
    }
    plan = RouteSyncPlan(
        site=site,
        discovered=seen,
        active_before=sum(1 for route in known.values() if route.is_active),
    )
    for slug in seen:
        route = known.get(slug)
        if route is None:
            plan.added.append(slug)
        elif route.is_active:
            plan.unchanged.append(slug)
        else:
            plan.returned.append(slug)
    found = set(seen)
    plan.missing = [slug for slug, route in known.items() if route.is_active and slug not in found]
    return plan


def apply_route_sync(
    session: Session,
    plan: RouteSyncPlan,
    *,
    allow_shrink: bool = False,
    now: datetime | None = None,
) -> None:
    """Записать план. Commit — за вызывающим.

    Раздел, пропавший с сайта, выключается, а не удаляется; вернувшийся —
    включается обратно. ``allow_shrink`` снимает отказ по доле исчезнувших
    разделов, но не по пустому списку: пустой каталог не записывается никогда.
    """
    reason = plan.refusal()
    if reason is not None and (not allow_shrink or not plan.discovered):
        raise RouteSyncRefused(f"{plan.site}: {reason}")

    moment = now or utcnow()
    known = {
        route.slug: route
        for route in session.scalars(select(SiteRoute).where(SiteRoute.site == plan.site)).all()
    }
    for slug in plan.discovered:
        route = known.get(slug)
        if route is None:
            session.add(
                SiteRoute(
                    site=plan.site,
                    slug=slug,
                    is_active=True,
                    first_seen_at=moment,
                    last_seen_at=moment,
                )
            )
        else:
            route.is_active = True
            route.last_seen_at = moment
    for slug in plan.missing:
        known[slug].is_active = False
    session.flush()
