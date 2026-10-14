from datetime import timedelta

from sqlalchemy import select

from conftest import audit_actions, balance
from funds_transfer.models import Transfer, VerificationChallenge, utcnow
from test_api import API, add_bob_payee, transfer


def pay(client, h, accounts, p, amount, key):
    return transfer(client, h, {"from_account_id": accounts["4012345"]["id"], "payee_id": p["id"],
                                "amount": amount}, key=key)


def verify(client, h, tid, code):
    return client.post(f"{API}/transfers/{tid}/verify", json={"code": code}, headers=h)


def wrong(code):
    return "000000" if code != "000000" else "111111"


def test_ac1_small_transfer_immediate(client, login, accounts, db, otp):
    h = login("alice")
    r = pay(client, h, accounts, add_bob_payee(client, h), "200.00", "a")
    assert r.status_code == 201 and r.json()["status"] == "completed" and not otp.codes
    assert balance(db, "4012345") == 480000


def test_ac2_step_up_then_verify(client, login, accounts, db, otp):
    h = login("alice")
    r = pay(client, h, accounts, add_bob_payee(client, h), "1500.00", "a")
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending_verification" and set(body["verification"]) == {"challenge_id", "expires_at"}
    assert balance(db, "4012345") == 500000
    v = verify(client, h, body["id"], otp.codes[-1])
    assert v.status_code == 200 and v.json()["status"] == "completed"
    assert balance(db, "4012345") == 350000
    assert balance(db, "4019876") == 400000
    assert verify(client, h, body["id"], otp.codes[-1]).json()["error"]["code"] == "TRANSFER_NOT_PENDING"
    assert {"step_up_challenge_issued", "step_up_verified"} <= set(audit_actions(db))


def test_ac3_three_wrong_codes_cancel(client, login, accounts, db, otp):
    h = login("alice")
    t = pay(client, h, accounts, add_bob_payee(client, h), "1500.00", "a").json()
    bad = wrong(otp.codes[-1])
    for _ in range(3):
        r = verify(client, h, t["id"], bad)
        assert r.status_code == 422 and r.json()["error"]["code"] == "VERIFICATION_FAILED"
    r = verify(client, h, t["id"], otp.codes[-1])
    assert r.json()["error"]["code"] == "TRANSFER_NOT_PENDING"
    assert client.get(f"{API}/transfers/{t['id']}", headers=h).json()["status"] == "cancelled"
    assert balance(db, "4012345") == 500000


def test_ac4_expired(client, login, accounts, db, otp):
    h = login("alice")
    t = pay(client, h, accounts, add_bob_payee(client, h), "1500.00", "a").json()
    ch = db.scalar(select(VerificationChallenge))
    ch.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    r = verify(client, h, t["id"], otp.codes[-1])
    assert r.json()["error"]["code"] == "VERIFICATION_EXPIRED"
    assert balance(db, "4012345") == 500000
    assert db.scalar(select(Transfer.status).where(Transfer.id == t["id"])) == "expired"


def test_ac5_daily_limit(client, login, accounts, db, otp):
    h = login("alice")
    p = add_bob_payee(client, h)
    for i in range(2):
        t = pay(client, h, accounts, p, "1250.00", f"k{i}")
        assert t.status_code == 202
        assert verify(client, h, t.json()["id"], otp.codes[-1]).status_code == 200
    r = pay(client, h, accounts, p, "600.00", "k3")
    assert r.status_code == 422 and r.json()["error"]["code"] == "DAILY_LIMIT_EXCEEDED"
    assert "transfer_limit_exceeded" in audit_actions(db)


def test_ac6_own_account_unrestricted(client, login, accounts, otp):
    h = login("alice")
    r = transfer(client, h, {"from_account_id": accounts["4012346"]["id"],
                             "to_account_id": accounts["4012345"]["id"], "amount": "10000.00"})
    assert r.status_code == 201 and not otp.codes
    assert client.get(f"{API}/limits", headers=h).json()["used_today"] == "0.00"


def test_ac7_limits_reflect_pending(client, login, accounts, otp):
    h = login("alice")
    p = add_bob_payee(client, h)
    assert client.get(f"{API}/limits", headers=h).json() == {
        "daily_payee_limit": "3000.00", "used_today": "0.00", "remaining_today": "3000.00",
        "step_up_threshold": "1000.00"}
    pay(client, h, accounts, p, "200.00", "a")
    pay(client, h, accounts, p, "1500.00", "b")
    lim = client.get(f"{API}/limits", headers=h).json()
    assert lim["used_today"] == "1700.00" and lim["remaining_today"] == "1300.00"


def test_cannot_verify_other_users_transfer(client, login, accounts, otp):
    t = pay(client, login("alice"), accounts, add_bob_payee(client, login("alice")), "1500.00", "a").json()
    assert verify(client, login("bob"), t["id"], otp.codes[-1]).status_code == 404
