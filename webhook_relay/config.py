"""Env-driven settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configurable knobs. Override via env vars prefixed ``WHR_``."""

    model_config = SettingsConfigDict(env_prefix="WHR_", env_file=".env", extra="ignore")

    # Storage
    database_url: str = "sqlite:///webhook_relay.db"

    # Delivery
    max_attempts: int = 6
    initial_backoff_seconds: float = 2.0
    max_backoff_seconds: float = 600.0
    backoff_multiplier: float = 2.5
    jitter_fraction: float = 0.25
    request_timeout_seconds: float = 10.0

    # Worker
    worker_poll_interval_seconds: float = 1.0
    worker_batch_size: int = 25

    # Signing
    signing_secret: str = "change-me-in-prod"
    signature_header: str = "X-Webhook-Signature"
    signature_algorithm: str = "sha256"
    timestamp_header: str = "X-Webhook-Timestamp"
    # Maximum allowed clock skew when verifying signatures.
    max_signature_age_seconds: int = 5 * 60
