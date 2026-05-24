# webhook-relay

A small, reliable **webhook delivery service**. Accepts events over HTTP, signs and delivers them to registered endpoints with **exponential backoff**, **HMAC signatures**, and a **dead-letter queue** for permanent failures.

The pattern shows up everywhere — Stripe, GitHub, Linear, Shopify, every payment processor I've seen. This is a focused open-source implementation you can drop into your service mesh, learn from, or extend.

## What it does

- **Inbox + outbox** — events you `POST /events` are queued in SQLite, then delivered by a background worker.
- **Exponential backoff + jitter** — configurable initial/max/multiplier with random jitter to avoid retry-storms.
- **Per-attempt persistence** — every attempt is recorded (status code, response snippet, error, duration) so you can audit failures.
- **HMAC signing** — every outbound request carries `X-Webhook-Signature: sha256=<hex>` and `X-Webhook-Timestamp: <unix-ts>`. Receivers verify both, rejecting tampered bodies and replays.
- **Dead-letter queue** — events exceeding `max_attempts` or returning a non-retryable status (4xx ≠ 408/429) move to `dead_lettered`. You can list them, inspect, and replay.
- **Replay API** — `POST /events/{id}/replay` resets a dead-lettered or pending event to `pending` for another shot.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run the service (port 8000, sqlite in cwd)
WHR_SIGNING_SECRET="my-secret" webhook-relay
```

In another shell:

```bash
# Register a target endpoint
curl -XPOST localhost:8000/endpoints \
  -H 'content-type: application/json' \
  -d '{"id":"orders","url":"https://httpbin.org/post"}'

# Enqueue an event
curl -XPOST localhost:8000/events \
  -H 'content-type: application/json' \
  -d '{"endpoint_id":"orders","event_type":"order.created","payload":{"order_id":42}}'
# -> {"event_id":"evt_abc123...","enqueued_at":"..."}

# Look up status
curl localhost:8000/events/evt_abc123...
# -> {... "status":"succeeded", "attempts":1 ...}

# Stats by status
curl localhost:8000/stats
# -> {"pending":0,"in_flight":0,"succeeded":1,"failed":0,"dead_lettered":0}
```

## API

| Method | Path                          | Purpose                                |
|---     |---                            |---                                     |
| POST   | `/endpoints`                  | Register or update a delivery endpoint |
| GET    | `/endpoints`                  | List endpoints                         |
| GET    | `/endpoints/{id}`             | Get one endpoint                       |
| POST   | `/events`                     | Enqueue an event for delivery          |
| GET    | `/events/{id}`                | Get event state                        |
| GET    | `/events/{id}/attempts`       | List all delivery attempts for an event|
| POST   | `/events/{id}/replay`         | Reset event to pending                 |
| GET    | `/stats`                      | Count of events by status              |
| GET    | `/healthz`                    | Liveness                                |

## Signatures (receiver-side)

Outbound POSTs carry:

```http
POST /your/endpoint HTTP/1.1
Content-Type: application/json
X-Webhook-Signature: sha256=8a8aef0ef4b…  ← HMAC of "<ts>.<body>"
X-Webhook-Timestamp: 1716592034           ← unix seconds
X-Event-Id: evt_abc123
X-Event-Type: order.created
X-Attempt: 2

{"order_id": 42}
```

To verify in Python (this same code is in `webhook_relay/signatures.py`):

```python
import hmac, hashlib, time

def verify(body: bytes, secret: str, sig_header: str, ts_header: str, *, max_age=300) -> bool:
    if abs(time.time() - int(ts_header)) > max_age:
        return False
    algo, expected = sig_header.split("=", 1)
    actual = hmac.new(
        secret.encode(), f"{ts_header}.".encode() + body, getattr(hashlib, algo)
    ).hexdigest()
    return hmac.compare_digest(actual, expected)
```

In Go / Node / Rust, it's the same shape: `HMAC-SHA256(secret, f"{ts}.{body}")`.

## Retry semantics

| Response                          | Retried?                                                          |
|---                                |---                                                                |
| `2xx`                             | No — success.                                                      |
| `408 Request Timeout`             | Yes.                                                              |
| `429 Too Many Requests`           | Yes.                                                              |
| Other `4xx`                       | No — dead-letter immediately. (Bug on sender side, not transient.)|
| `5xx`                             | Yes.                                                              |
| Connection error / timeout        | Yes.                                                              |

Backoff is `initial * (multiplier ** (attempt - 1))` capped by `max_backoff`, then ±`jitter_fraction` uniform jitter on top. With defaults (`2.0s × 2.5^n`, jitter 0.25, max 600s), retries land at roughly: 2s, 5s, 12.5s, 31s, 78s, 195s, then capped.

Default `max_attempts=6` (≈14 minutes total wall time before DLQ).

## Configuration

All settings come from env vars prefixed `WHR_`:

| Var                                  | Default                    | Notes                                |
|---                                   |---                         |---                                   |
| `WHR_DATABASE_URL`                   | `sqlite:///webhook_relay.db` | Currently SQLite-only. Postgres on roadmap. |
| `WHR_SIGNING_SECRET`                 | `change-me-in-prod`         | HMAC secret. **Set it.**             |
| `WHR_MAX_ATTEMPTS`                   | `6`                         | Including the first delivery.        |
| `WHR_INITIAL_BACKOFF_SECONDS`        | `2.0`                       |                                      |
| `WHR_BACKOFF_MULTIPLIER`             | `2.5`                       |                                      |
| `WHR_MAX_BACKOFF_SECONDS`            | `600`                       |                                      |
| `WHR_JITTER_FRACTION`                | `0.25`                      | 0 disables.                          |
| `WHR_REQUEST_TIMEOUT_SECONDS`        | `10.0`                      | Per-attempt HTTP timeout.            |
| `WHR_WORKER_POLL_INTERVAL_SECONDS`   | `1.0`                       |                                      |
| `WHR_WORKER_BATCH_SIZE`              | `25`                        | Max events claimed per tick.         |
| `WHR_SIGNATURE_HEADER`               | `X-Webhook-Signature`       |                                      |
| `WHR_SIGNATURE_ALGORITHM`            | `sha256`                    | `sha256` or `sha512`.                |

## Docker

```bash
docker compose up --build
```

Listens on `:8000`. State persists in a named volume `relay-data`.

## Architecture

```
                                                         retry queue
                                                          (sqlite)
  POST /events                                              ↑   ↓
  ──────────────►  FastAPI  ────► enqueue ────► events ───►worker────► HTTP POST ──► target endpoint
                       │                                     │             │
                       │                                     ↓             ↓
                       └──── GET /events/{id} ──────► attempts log    record outcome
                                                                       (status, body, dur)
```

- **FastAPI** = sync admin/inbox API.
- **Worker** = single asyncio task in the same process (you can run N processes if you want; SQLite serialises writes well enough for low-to-mid traffic).
- **Storage** = SQLite with `(status, next_attempt_at)` indexed. Trivial to swap for Postgres.

## Roadmap

- Postgres backend (with `SELECT ... FOR UPDATE SKIP LOCKED` for multi-worker scaling).
- Webhook **filtering** — let endpoints subscribe to specific `event_type` patterns.
- **Prometheus** metrics endpoint.
- **Multi-process worker** with leader election.
- Configurable retry policy per endpoint.

## License

MIT.
