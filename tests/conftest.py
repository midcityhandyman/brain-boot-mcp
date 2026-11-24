from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brain_boot_mcp.seed import seed
from brain_boot_mcp.service import BrainService
from brain_boot_mcp.store_sqlite import SqliteStore

START = datetime(2026, 1, 5, 14, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, start: datetime = START) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current = self.current + timedelta(**delta)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def service(clock: FakeClock):
    store = SqliteStore(":memory:")
    store.init_schema()
    yield BrainService(store, clock=clock)
    store.close()


@pytest.fixture
def seeded(service: BrainService) -> BrainService:
    seed(service)
    return service
