"""HTTP delivery + backoff logic."""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import httpx

from webhook_relay.config import Settings
from webhook_relay.models import WebhookEvent
from webhook_relay.signatures import sign


def backoff_seconds(attempt: int, settings: Settings) -> float:
    """Exponential backoff with jitter.

    ``attempt`` is 1-indexed: first retry uses initial_backoff_seconds.
    """
    base = settings.initial_backoff_seconds * (settings.backoff_multiplier ** (attempt - 1))
    base = min(base, settings.max_backoff_seconds)
    if settings.jitter_fraction > 0:
        spread = base * settings.jitter_fraction
        return max(0.0, base + random.uniform(-spread, spread))
    return base


def next_attempt_at(now: datetime, attempt: int, settings: Settings) -> datetime:
    return now + timedelta(seconds=backoff_seconds(attempt, settings))


def should_retry(status_code: int | None) -> bool:
    """5xx, 408, 429 and connection errors (status_code=None) are retryable."""
    if status_code is None:
        return True
    if status_code in {408, 429}:
        return True
    if 500 <= status_code <= 599:
        return True
    return False


HttpSendFn = Callable[
    [str, dict[str, str], bytes],
    Awaitable[httpx.Response],
]


async def deliver_one(
    event: WebhookEvent,
    endpoint_url: str,
    settings: Settings,
    *,
    http_send: HttpSendFn | None = None,
) -> tuple[bool, int | None, str | None, str | None, int]:
    """Attempt a single delivery.

    Returns: ``(succeeded, status_code, response_body_truncated, error, duration_ms)``.
    """
    body = json.dumps(event.payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signed = sign(body, settings.signing_secret, algorithm=settings.signature_algorithm)

    headers = {
        "content-type": "application/json",
        settings.signature_header: signed.header_value,
        settings.timestamp_header: str(signed.timestamp),
        "x-event-id": event.id,
        "x-event-type": event.event_type,
        "x-attempt": str(event.attempts + 1),
    }

    started = time.monotonic()
    try:
        if http_send is not None:
            response = await http_send(endpoint_url, headers, body)
        else:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                response = await client.post(endpoint_url, content=body, headers=headers)
        dur_ms = int((time.monotonic() - started) * 1000)
        # Truncate response body so a chatty target doesn't blow up storage.
        snippet = response.text[:1024]
        ok = 200 <= response.status_code < 300
        return ok, response.status_code, snippet, None, dur_ms
    except httpx.HTTPError as e:
        dur_ms = int((time.monotonic() - started) * 1000)
        return False, None, None, f"{type(e).__name__}: {e}", dur_ms


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
