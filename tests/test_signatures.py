"""Signature roundtrip + tamper detection."""

from __future__ import annotations

from webhook_relay.signatures import sign, verify


def test_roundtrip_sha256() -> None:
    body = b'{"hello":"world"}'
    p = sign(body, "shh", ts=1_000_000_000)
    ok, reason = verify(
        body,
        "shh",
        signature_header=p.header_value,
        timestamp_header=p.timestamp,
        now=1_000_000_000,
    )
    assert ok, reason


def test_tampered_body_rejected() -> None:
    body = b'{"hello":"world"}'
    p = sign(body, "shh", ts=1_000_000_000)
    ok, reason = verify(
        b'{"hello":"evil"}',
        "shh",
        signature_header=p.header_value,
        timestamp_header=p.timestamp,
        now=1_000_000_000,
    )
    assert not ok
    assert "mismatch" in reason  # type: ignore[operator]


def test_stale_timestamp_rejected() -> None:
    body = b"hi"
    p = sign(body, "shh", ts=1_000_000_000)
    ok, reason = verify(
        body,
        "shh",
        signature_header=p.header_value,
        timestamp_header=p.timestamp,
        max_age_seconds=300,
        now=1_000_000_400,  # 400s later
    )
    assert not ok
    assert "out of range" in reason  # type: ignore[operator]


def test_wrong_secret_rejected() -> None:
    body = b"x"
    p = sign(body, "shh", ts=1_000_000_000)
    ok, _ = verify(
        body,
        "other-secret",
        signature_header=p.header_value,
        timestamp_header=p.timestamp,
        now=1_000_000_000,
    )
    assert not ok


def test_unknown_algorithm_rejected() -> None:
    ok, reason = verify(
        b"x",
        "shh",
        signature_header="md5=deadbeef",
        timestamp_header=1_000_000_000,
        now=1_000_000_000,
    )
    assert not ok
    assert "unsupported" in reason  # type: ignore[operator]
