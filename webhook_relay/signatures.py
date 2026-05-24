"""HMAC signing & verification for webhook payloads.

Mirrors the Stripe / GitHub style: ``<algo>=<hex>``. Includes a timestamp
header so old captured requests can't be replayed past a configurable window.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class SignedPayload:
    body: bytes
    timestamp: int
    signature: str
    header_value: str  # the full string to put in X-Webhook-Signature


def sign(
    body: bytes, secret: str, algorithm: str = "sha256", ts: int | None = None
) -> SignedPayload:
    """Compute an HMAC signature.

    The signed string is ``f"{timestamp}.{body.decode('utf-8', 'surrogateescape')}"``
    so that a fixed timestamp can't be replayed with a different body. We
    surrogate-escape so binary bodies don't blow up.
    """
    if algorithm not in {"sha256", "sha512"}:
        raise ValueError(f"unsupported algorithm: {algorithm}")
    if ts is None:
        ts = int(time.time())
    signed_str = f"{ts}.".encode() + body
    digest = hmac.new(secret.encode("utf-8"), signed_str, getattr(hashlib, algorithm)).hexdigest()
    sig = f"{algorithm}={digest}"
    return SignedPayload(body=body, timestamp=ts, signature=digest, header_value=sig)


def verify(
    body: bytes,
    secret: str,
    *,
    signature_header: str,
    timestamp_header: str | int,
    max_age_seconds: int = 300,
    now: int | None = None,
) -> tuple[bool, str | None]:
    """Verify an inbound signature.

    Returns ``(ok, reason)``. ``reason`` is set when verification fails.
    """
    if now is None:
        now = int(time.time())

    try:
        ts = int(timestamp_header)
    except (TypeError, ValueError):
        return False, "missing or invalid timestamp"

    if abs(now - ts) > max_age_seconds:
        return False, "signature timestamp out of range"

    if "=" not in signature_header:
        return False, "signature missing algorithm prefix"
    algo, hexsig = signature_header.split("=", 1)
    if algo not in {"sha256", "sha512"}:
        return False, f"unsupported algorithm: {algo}"

    signed_str = f"{ts}.".encode() + body
    expected = hmac.new(secret.encode("utf-8"), signed_str, getattr(hashlib, algo)).hexdigest()
    if not hmac.compare_digest(expected, hexsig.strip()):
        return False, "signature mismatch"
    return True, None
