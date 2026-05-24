"""Shared test fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from webhook_relay.config import Settings
from webhook_relay.storage import Storage


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        max_attempts=4,
        initial_backoff_seconds=0.01,
        backoff_multiplier=2.0,
        max_backoff_seconds=1.0,
        jitter_fraction=0.0,
        worker_poll_interval_seconds=0.05,
        signing_secret="test-secret",
    )


@pytest_asyncio.fixture
async def storage(settings: Settings) -> Storage:
    s = Storage(settings.database_url)
    await s.init()
    return s


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
