"""FastAPI app — admin endpoints + the enqueue API."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse

from webhook_relay.config import Settings
from webhook_relay.delivery import now_utc
from webhook_relay.models import (
    CreateEndpointRequest,
    DeliveryAttempt,
    Endpoint,
    EnqueueEventRequest,
    EnqueueEventResponse,
    WebhookEvent,
)
from webhook_relay.storage import Storage
from webhook_relay.worker import start_worker_loop


def create_app(settings: Settings | None = None, *, run_worker: bool = True) -> FastAPI:
    settings = settings or Settings()
    storage = Storage(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await storage.init()
        worker_task: asyncio.Task | None = None
        if run_worker:
            worker_task = asyncio.create_task(start_worker_loop(storage, settings))
        try:
            yield
        finally:
            if worker_task is not None:
                worker_task.cancel()
                try:
                    await worker_task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(
        title="webhook-relay",
        version="0.1.0",
        description="Reliable webhook delivery with retries + DLQ.",
        lifespan=lifespan,
    )

    def get_storage() -> Storage:
        return storage

    def get_settings() -> Settings:
        return settings

    # ---- health/metrics -----------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/stats")
    async def stats(s: Storage = Depends(get_storage)) -> dict:
        return await s.stats()

    # ---- endpoints ----------------------------------------------------------

    @app.post("/endpoints", status_code=status.HTTP_201_CREATED, response_model=Endpoint)
    async def create_endpoint(req: CreateEndpointRequest, s: Storage = Depends(get_storage)):
        ep = Endpoint(id=req.id, url=req.url, description=req.description)
        await s.upsert_endpoint(ep)
        return ep

    @app.get("/endpoints", response_model=list[Endpoint])
    async def list_endpoints(s: Storage = Depends(get_storage)):
        return await s.list_endpoints()

    @app.get("/endpoints/{endpoint_id}", response_model=Endpoint)
    async def get_endpoint(endpoint_id: str, s: Storage = Depends(get_storage)):
        ep = await s.get_endpoint(endpoint_id)
        if not ep:
            raise HTTPException(404, "endpoint not found")
        return ep

    # ---- events -------------------------------------------------------------

    @app.post("/events", response_model=EnqueueEventResponse)
    async def enqueue_event(req: EnqueueEventRequest, s: Storage = Depends(get_storage)):
        if not await s.get_endpoint(req.endpoint_id):
            raise HTTPException(400, f"endpoint {req.endpoint_id} not registered")
        now = now_utc()
        event = await s.enqueue(req.endpoint_id, req.event_type, req.payload, now=now)
        return EnqueueEventResponse(event_id=event.id, enqueued_at=now)

    @app.get("/events/{event_id}", response_model=WebhookEvent)
    async def get_event(event_id: str, s: Storage = Depends(get_storage)):
        e = await s.get_event(event_id)
        if not e:
            raise HTTPException(404, "event not found")
        return e

    @app.get("/events/{event_id}/attempts", response_model=list[DeliveryAttempt])
    async def list_attempts(event_id: str, s: Storage = Depends(get_storage)):
        return await s.list_attempts(event_id)

    @app.post("/events/{event_id}/replay")
    async def replay_event(event_id: str, s: Storage = Depends(get_storage)):
        if not await s.replay(event_id, now_utc()):
            raise HTTPException(404, "event not found")
        return JSONResponse({"replayed": event_id})

    return app
