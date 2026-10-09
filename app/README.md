# Funds Transfer Service (v1.0 baseline)

FastAPI + SQLAlchemy (SQLite) implementation of `spec/funds-transfer-spec.md`.
CR-001 and CR-002 are not implemented.

## Run

```bash
cd app
pip install -r requirements.txt
export JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
uvicorn funds_transfer.asgi:app --reload
```

Swagger UI: http://localhost:8000/docs. Get a token via `POST /api/v1/auth/token`, then use **Authorize**.

| Env var | Default | Notes |
|---|---|---|
| `JWT_SECRET` | required | at least 32 characters |
| `DATABASE_URL` | `sqlite:///./funds_transfer.db` | |
| `TOKEN_TTL_MINUTES` | `15` | |

## Seed users (local development only)

| Username | Password | Role |
|---|---|---|
| `alice` | `alice-dev-pw` | customer (chequing 4012345, savings 4012346) |
| `bob` | `bob-dev-pw` | customer (chequing 4019876) |
| `sam` | `sam-dev-pw` | support_agent |

## Test

```bash
cd app && python3 -m pytest
```

## Notes

- Amounts are integer cents internally and `"125.50"` strings in the API.
- Failed transfers (e.g. `INSUFFICIENT_FUNDS`) are audited but not persisted as `Transfer` rows.
- Support agents see masked account numbers; transfer detail is visible only to the creating customer.
