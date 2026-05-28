"""Onboarding wizard — определяет состояние первого запуска и ведёт через 3 шага.

Состояния (в порядке):
1. **empty_db**: ни одного прогона / Product / Category — показываем wizard
2. **need_categories**: есть Run, но нет Category — нужно добавить
3. **need_products_or_recipients**: всё есть, но БД пустая (sample / production)
4. **ready**: всё настроено — обычный режим

Wizard рендерится на главной до тех пор пока состояние != ready.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from src.storage import (
    Category,
    Product,
    Recipient,
    Run,
    TrackedProduct,
)

State = Literal["empty_db", "need_categories", "need_recipients", "ready"]


@dataclass
class OnboardingStatus:
    state: State
    has_runs: bool
    has_categories: bool
    has_recipients: bool
    has_products: bool
    has_tracked: bool


def get_status(session: Session) -> OnboardingStatus:
    has_runs = session.scalar(select(func.count(Run.id))) > 0
    has_categories = session.scalar(select(func.count(Category.id))) > 0
    has_recipients = (
        session.scalar(select(func.count(Recipient.id)).where(Recipient.is_active.is_(True))) > 0
    )
    has_products = session.scalar(select(func.count(Product.id))) > 0
    has_tracked = session.scalar(select(func.count(TrackedProduct.id))) > 0

    if not has_runs and not has_categories and not has_products:
        state: State = "empty_db"
    elif not has_categories and not has_tracked:
        state = "need_categories"
    elif not has_recipients:
        state = "need_recipients"
    else:
        state = "ready"

    return OnboardingStatus(
        state=state,
        has_runs=has_runs,
        has_categories=has_categories,
        has_recipients=has_recipients,
        has_products=has_products,
        has_tracked=has_tracked,
    )


def is_complete(session: Session) -> bool:
    return get_status(session).state == "ready"
