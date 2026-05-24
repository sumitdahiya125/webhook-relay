"""Delivery + worker tests with an in-process httpx mock."""

from __future__ import annotations

import httpx
import pytest

from webhook_relay.config import Settings
from webhook_relay.delivery import backoff_seconds, should_retry, now_utc
from webhook_relay.models import Endpoint, DeliveryStatus
from webhook_relay.storage import Storage
from webhook_relay.worker import tick


def test_backoff_grows_exponentially() -> None:
    s = Settings(
        initial_backoff_seconds=1.0,
        backoff_multiplier=2.0,
        jitter_fraction=0.0,
        max_backoff_seconds=1_000.0,
    )
    assert backoff_seconds(1, s) == 1.0
    assert backoff_seconds(2, s) == 2.0
    assert backoff_seconds(3, s) == 4.0
    assert backoff_seconds(10, s) == min(1.0 * 2.0**9, 1_000.0)


def test_backoff_capped_by_max() -> None:
    s = Settings(
        initial_backoff_seconds=100.0,
        backoff_multiplier=10.0,
        jitter_fraction=0.0,
        max_backoff_seconds=300.0,
    )
    assert backoff_seconds(1, s) == 100.0
    assert backoff_seconds(2, s) == 300.0
    assert backoff_seconds(5, s) == 300.0


def test_should_retry() -> None:
    assert should_retry(500)
    assert should_retry(502)
    assert should_retry(429)
    assert should_retry(408)
    assert should_retry(None)
    assert not should_retry(200)
    assert not should_retry(400)
    assert not should_retry(404)
    assert not should_retry(422)


def _send_factory(responses: list):
    """Build an HttpSendFn that returns canned responses in order."""
    idx = {"i": 0}

    async def fn(_url, _headers, _body):
        i = idx["i"]
        idx["i"] += 1
        item = responses[min(i, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item)

    return fn


@pytest.mark.asyncio
async def test_successful_delivery(storage: Storage, settings: Settings) -> None:
    await storage.upsert_endpoint(
        Endpoint(id="ep1", url="https://example.test/hook")
    )
    event = await storage.enqueue("ep1", "order.created", {"x": 1}, now=now_utc())
    send = _send_factory([200])
    processed = await tick(storage, settings, http_send=send)
    assert processed == 1
    e = await storage.get_event(event.id)
    assert e is not None and e.status == DeliveryStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_retry_then_succeed(storage: Storage, settings: Settings) -> None:
    await storage.upsert_endpoint(
        Endpoint(id="ep1", url="https://example.test/hook")
    )
    event = await storage.enqueue("ep1", "order.created", {"x": 1}, now=now_utc())

    # First two attempts return 500, third returns 200.
    send = _send_factory([500, 500, 200])

    # Tick three times — each tick processes one due event. Backoff is tiny so
    # we sleep a hair between ticks to let next_attempt_at pass.
    import asyncio as aio

    for _ in range(3):
        n = await tick(storage, settings, http_send=send)
        if n == 0:
            await aio.sleep(0.05)
        await aio.sleep(0.05)

    e = await storage.get_event(event.id)
    assert e is not None
    assert e.status == DeliveryStatus.SUCCEEDED, f"got {e.status}, attempts={e.attempts}"
    assert e.attempts == 3


@pytest.mark.asyncio
async def test_dead_letter_after_max_attempts(storage: Storage, settings: Settings) -> None:
    await storage.upsert_endpoint(
        Endpoint(id="ep1", url="https://example.test/hook")
    )
    event = await storage.enqueue("ep1", "order.created", {"x": 1}, now=now_utc())

    send = _send_factory([500] * 10)  # always fail

    import asyncio as aio

    for _ in range(settings.max_attempts + 2):
        await tick(storage, settings, http_send=send)
        await aio.sleep(0.05)

    e = await storage.get_event(event.id)
    assert e is not None
    assert e.status == DeliveryStatus.DEAD_LETTERED, f"got {e.status}, attempts={e.attempts}"
    assert e.attempts == settings.max_attempts


@pytest.mark.asyncio
async def test_4xx_skips_retries(storage: Storage, settings: Settings) -> None:
    await storage.upsert_endpoint(
        Endpoint(id="ep1", url="https://example.test/hook")
    )
    event = await storage.enqueue("ep1", "order.created", {"x": 1}, now=now_utc())
    send = _send_factory([404])
    await tick(storage, settings, http_send=send)
    e = await storage.get_event(event.id)
    assert e is not None and e.status == DeliveryStatus.DEAD_LETTERED
    assert e.attempts == 1, "404 should go to DLQ after exactly one attempt"


@pytest.mark.asyncio
async def test_replay_resets_status(storage: Storage, settings: Settings) -> None:
    await storage.upsert_endpoint(
        Endpoint(id="ep1", url="https://example.test/hook")
    )
    event = await storage.enqueue("ep1", "order.created", {"x": 1}, now=now_utc())
    send = _send_factory([404])
    await tick(storage, settings, http_send=send)
    assert (await storage.get_event(event.id)).status == DeliveryStatus.DEAD_LETTERED  # type: ignore[union-attr]

    ok = await storage.replay(event.id, now_utc())
    assert ok
    e = await storage.get_event(event.id)
    assert e is not None
    assert e.status == DeliveryStatus.PENDING
    assert e.attempts == 0


@pytest.mark.asyncio
async def test_attempts_log_written(storage: Storage, settings: Settings) -> None:
    await storage.upsert_endpoint(
        Endpoint(id="ep1", url="https://example.test/hook")
    )
    event = await storage.enqueue("ep1", "order.created", {"x": 1}, now=now_utc())
    send = _send_factory([200])
    await tick(storage, settings, http_send=send)
    attempts = await storage.list_attempts(event.id)
    assert len(attempts) == 1
    assert attempts[0].status_code == 200
