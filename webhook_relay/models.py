"""Domain types."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, HttpUrl


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # transient — will retry
    DEAD_LETTERED = "dead_lettered"


class WebhookEvent(BaseModel):
    id: str
    endpoint_id: str
    event_type: str
    payload: dict
    status: DeliveryStatus
    attempts: int
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None
    last_status_code: int | None = None


class DeliveryAttempt(BaseModel):
    id: int
    event_id: str
    attempted_at: datetime
    status_code: int | None
    response_body: str | None
    error: str | None
    duration_ms: int


class Endpoint(BaseModel):
    id: str
    url: HttpUrl
    description: str | None = None
    active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ----- request/response DTOs ------------------------------------------------


class CreateEndpointRequest(BaseModel):
    id: str
    url: HttpUrl
    description: str | None = None


class EnqueueEventRequest(BaseModel):
    endpoint_id: str
    event_type: str
    payload: dict


class EnqueueEventResponse(BaseModel):
    event_id: str
    enqueued_at: datetime
