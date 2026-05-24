"""Background worker loop — polls storage for due events, delivers them."""

from __future__ import annotations

import asyncio
import logging

from webhook_relay.config import Settings
from webhook_relay.delivery import (
    HttpSendFn,
    deliver_one,
    next_attempt_at,
    now_utc,
    should_retry,
)
from webhook_relay.storage import Storage

log = logging.getLogger(__name__)


async def tick(storage: Storage, settings: Settings, *, http_send: HttpSendFn | None = None) -> int:
    """Run one delivery tick. Returns the number of events processed."""
    now = now_utc()
    batch = await storage.claim_due(now, settings.worker_batch_size)
    if not batch:
        return 0

    processed = 0
    for event in batch:
        endpoint = await storage.get_endpoint(event.endpoint_id)
        if endpoint is None or not endpoint.active:
            await storage.mark_failed(
                event.id,
                now=now,
                next_attempt_at=now,
                last_error=f"endpoint {event.endpoint_id} missing or inactive",
                last_status_code=None,
                dead_letter=True,
            )
            await storage.record_attempt(
                event.id, now, None, None, "endpoint missing or inactive", 0
            )
            processed += 1
            continue

        ok, status, body_snippet, error, dur_ms = await deliver_one(
            event, str(endpoint.url), settings, http_send=http_send
        )
        await storage.record_attempt(event.id, now_utc(), status, body_snippet, error, dur_ms)

        if ok:
            await storage.mark_succeeded(event.id, now_utc())
        else:
            attempts_used = event.attempts + 1
            if attempts_used >= settings.max_attempts or not should_retry(status):
                await storage.mark_failed(
                    event.id,
                    now=now_utc(),
                    next_attempt_at=now_utc(),
                    last_error=error or f"non-retryable status {status}",
                    last_status_code=status,
                    dead_letter=True,
                )
            else:
                await storage.mark_failed(
                    event.id,
                    now=now_utc(),
                    next_attempt_at=next_attempt_at(now_utc(), attempts_used, settings),
                    last_error=error,
                    last_status_code=status,
                    dead_letter=False,
                )
        processed += 1
    return processed


async def start_worker_loop(storage: Storage, settings: Settings) -> None:
    """Long-running poll loop. Cancel the task to stop."""
    log.info("starting webhook-relay worker (poll=%.1fs)", settings.worker_poll_interval_seconds)
    while True:
        try:
            processed = await tick(storage, settings)
            if processed == 0:
                await asyncio.sleep(settings.worker_poll_interval_seconds)
        except asyncio.CancelledError:
            log.info("worker cancelled, exiting")
            raise
        except Exception:
            log.exception("worker tick failed")
            await asyncio.sleep(settings.worker_poll_interval_seconds)
