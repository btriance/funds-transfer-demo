import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from funds_transfer.config import Settings
from funds_transfer.main import create_app
from funds_transfer.models import Account, AuditEvent

PASSWORDS = {"alice": "alice-dev-pw", "bob": "bob-dev-pw", "sam": "sam-dev-pw"}


@pytest.fixture()
def otp():
    return CapturingOtpSender()


@pytest.fixture()
def client(otp):
    settings = Settings(jwt_secret="test-secret-test-secret-test-secret!", database_url="sqlite://",
                        token_ttl_minutes=15)
    app = create_app(settings, otp_sender=otp)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db(client):
    s = client.app.state.session_factory()
    yield s
    s.close()


@pytest.fixture()
def login(client):
    def _login(user):
        r = client.post("/api/v1/auth/token", json={"username": user, "password": PASSWORDS[user]})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}
    return _login


@pytest.fixture()
def accounts(client, login):
    """Map of account number -> account dict as seen by each owner."""
    out = {}
    for u in ("alice", "bob"):
        for a in client.get("/api/v1/accounts", headers=login(u)).json():
            out[a["account_number"]] = a
    return out


def balance(db, number):
    db.expire_all()
    return db.scalar(select(Account.balance_cents).where(Account.account_number == number))


def audit_actions(db):
    db.expire_all()
    return [e.action for e in db.scalars(select(AuditEvent))]


class CapturingOtpSender:
    def __init__(self):
        self.codes = []

    def send(self, user_id, code):
        self.codes.append(code)
