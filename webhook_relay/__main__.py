"""Entry point: ``webhook-relay`` command or ``python -m webhook_relay``."""

from __future__ import annotations

import logging

import uvicorn

from webhook_relay.config import Settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    settings = Settings()
    uvicorn.run(
        "webhook_relay.api:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
