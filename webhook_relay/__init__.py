"""webhook-relay — a reliable webhook delivery service.

See the project README for usage. The public API is small:

- ``webhook_relay.api.create_app``: builds the FastAPI app.
- ``webhook_relay.worker.start_worker_loop``: runs the delivery worker.
- ``webhook_relay.config.Settings``: the env-driven settings model.
"""

from webhook_relay.config import Settings
from webhook_relay.models import (
    DeliveryAttempt,
    DeliveryStatus,
    Endpoint,
    WebhookEvent,
)

__all__ = [
    "DeliveryAttempt",
    "DeliveryStatus",
    "Endpoint",
    "Settings",
    "WebhookEvent",
]

__version__ = "0.1.0"
