"""Bounded producer-lock waiting used by the isolated recovery workflow."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from src import run_lock


def test_wait_for_exclusive_scrape_lock_retries_then_holds_lock(monkeypatch) -> None:
    outcomes = iter((False, True))
    probes: list[bool] = []
    sleeps: list[float] = []

    @contextmanager
    def fake_try_exclusive_scrape_lock(_session):
        acquired = next(outcomes)
        probes.append(acquired)
        yield acquired

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(run_lock, "try_exclusive_scrape_lock", fake_try_exclusive_scrape_lock)
    monkeypatch.setattr(run_lock.asyncio, "sleep", fake_sleep)

    async def exercise() -> None:
        async with run_lock.wait_for_exclusive_scrape_lock(
            object(),
            timeout_seconds=30,
            poll_seconds=5,
        ) as acquired:
            assert acquired is True

    asyncio.run(exercise())

    assert probes == [False, True]
    assert sleeps == [5]


def test_wait_for_exclusive_scrape_lock_fails_closed_after_deadline(monkeypatch) -> None:
    probes: list[bool] = []

    @contextmanager
    def fake_try_exclusive_scrape_lock(_session):
        probes.append(False)
        yield False

    monkeypatch.setattr(run_lock, "try_exclusive_scrape_lock", fake_try_exclusive_scrape_lock)

    async def exercise() -> None:
        async with run_lock.wait_for_exclusive_scrape_lock(
            object(),
            timeout_seconds=0,
        ) as acquired:
            assert acquired is False

    asyncio.run(exercise())

    assert probes == [False]


@pytest.mark.parametrize(
    ("timeout_seconds", "poll_seconds"),
    ((-1, 1), (1, 0)),
)
def test_wait_for_exclusive_scrape_lock_rejects_invalid_windows(
    timeout_seconds: float,
    poll_seconds: float,
) -> None:
    async def exercise() -> None:
        async with run_lock.wait_for_exclusive_scrape_lock(
            object(),
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        ):
            raise AssertionError("invalid window should not yield")

    with pytest.raises(ValueError):
        asyncio.run(exercise())
