from datetime import datetime, timedelta, timezone

import jwt

from conftest import audit_actions, balance

API = "/api/v1"


def transfer(client, headers, body, key="k1"):
    return client.post(f"{API}/transfers", json=body, headers={**headers, "Idempotency-Key": key})


def add_bob_payee(client, h):
    r = client.post(f"{API}/payees", json={"nickname": "Bob", "payee_name": "Bob Brown",
                                           "account_number": "4019876"}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


# ---- auth ----
def test_health_public(client):
    assert client.get(f"{API}/health").json() == {"status": "ok"}


def test_missing_and_bad_token(client):
    assert client.get(f"{API}/accounts").json()["error"]["code"] == "UNAUTHENTICATED"
    r = client.get(f"{API}/accounts", headers={"Authorization": "Bearer junk"})
    assert r.status_code == 401


def test_expired_token_rejected(client, db):
    from funds_transfer.models import User
    uid = db.query(User).filter_by(username="alice").one().id
    old = datetime.now(timezone.utc) - timedelta(minutes=1)
    tok = jwt.encode({"sub": uid, "role": "customer", "exp": old}, "test-secret-test-secret-test-secret!", "HS256")
    assert client.get(f"{API}/accounts", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_login_failure(client, db):
    r = client.post(f"{API}/auth/token", json={"username": "alice", "password": "wrong"})
    assert r.status_code == 401
    assert "login_failure" in audit_actions(db)


# ---- AC-1 ----
def test_ac1_own_account_transfer(client, login, accounts, db):
    h = login("alice")
    chq, sav = accounts["4012345"], accounts["4012346"]
    r = transfer(client, h, {"from_account_id": chq["id"], "to_account_id": sav["id"], "amount": "100.00"})
    assert r.status_code == 201, r.text
    assert balance(db, "4012345") == 490000 and balance(db, "4012346") == 2010000
    d = client.get(f"{API}/accounts/{chq['id']}/transactions", headers=h).json()["items"]
    c = client.get(f"{API}/accounts/{sav['id']}/transactions", headers=h).json()["items"]
    assert [(t["direction"], t["amount"], t["balance_after"]) for t in d] == [("debit", "100.00", "4900.00")]
    assert [(t["direction"], t["balance_after"]) for t in c] == [("credit", "20100.00")]
    assert "transfer_created" in audit_actions(db)


# ---- AC-2 ----
def test_ac2_payee_transfer(client, login, accounts, db):
    h = login("alice")
    p = add_bob_payee(client, h)
    assert p["account_number"] == "****9876"
    r = transfer(client, h, {"from_account_id": accounts["4012345"]["id"], "payee_id": p["id"],
                             "amount": "50.00", "memo": "hi"})
    assert r.status_code == 201
    assert balance(db, "4019876") == 255000
    assert client.get(f"{API}/transfers/{r.json()['id']}", headers=h).status_code == 200


# ---- AC-3 / BR-1 ----
def test_ac3_other_customers_account_404(client, login, accounts, db):
    h = login("alice")
    r = client.get(f"{API}/accounts/{accounts['4019876']['id']}", headers=h)
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert "access_denied" in audit_actions(db)


def test_cannot_transfer_from_others_account(client, login, accounts, db):
    r = transfer(client, login("alice"), {"from_account_id": accounts["4019876"]["id"],
                                          "to_account_id": accounts["4012345"]["id"], "amount": "1.00"})
    assert r.status_code == 404
    assert balance(db, "4019876") == 250000


def test_cannot_use_others_payee(client, login, accounts):
    p = add_bob_payee(client, login("alice"))
    r = transfer(client, login("bob"), {"from_account_id": accounts["4019876"]["id"], "payee_id": p["id"],
                                        "amount": "1.00"})
    assert r.status_code == 404
    assert client.delete(f"{API}/payees/{p['id']}", headers=login("bob")).status_code == 404


# ---- AC-4 / BR-4 ----
def test_ac4_insufficient_funds(client, login, accounts, db):
    r = transfer(client, login("alice"), {"from_account_id": accounts["4012345"]["id"],
                                          "to_account_id": accounts["4012346"]["id"], "amount": "5000.01"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INSUFFICIENT_FUNDS"
    assert balance(db, "4012345") == 500000 and balance(db, "4012346") == 2000000
    assert "transfer_failed" in audit_actions(db)


def test_frozen_account(client, login, accounts, db):
    from funds_transfer.models import Account
    db.query(Account).filter_by(account_number="4012345").update({"status": "frozen"})
    db.commit()
    r = transfer(client, login("alice"), {"from_account_id": accounts["4012345"]["id"],
                                          "to_account_id": accounts["4012346"]["id"], "amount": "1.00"})
    assert r.json()["error"]["code"] == "ACCOUNT_NOT_OPEN"


# ---- AC-5 / BR-6 ----
def test_ac5_idempotency(client, login, accounts, db):
    h = login("alice")
    body = {"from_account_id": accounts["4012345"]["id"], "to_account_id": accounts["4012346"]["id"],
            "amount": "10.00"}
    r1 = transfer(client, h, body, key="same")
    r2 = transfer(client, h, body, key="same")
    assert r2.status_code == 200 and r1.json()["id"] == r2.json()["id"]
    assert balance(db, "4012345") == 499000
    r3 = transfer(client, h, {**body, "amount": "11.00"}, key="same")
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_idempotency_key_required_and_scoped_per_user(client, login, accounts):
    h = login("alice")
    body = {"from_account_id": accounts["4012345"]["id"], "to_account_id": accounts["4012346"]["id"],
            "amount": "1.00"}
    assert client.post(f"{API}/transfers", json=body, headers=h).status_code == 422
    a = transfer(client, h, body, key="shared")
    b = transfer(client, login("bob"), {"from_account_id": accounts["4019876"]["id"],
                                        "to_account_id": accounts["4019876"]["id"], "amount": "1.00"}, key="shared")
    assert a.status_code == 201 and b.status_code == 422  # bob's own request fails validation, not replay


# ---- BR-3 / BR-9 validation ----
def test_amount_validation(client, login, accounts):
    h = login("alice")
    for bad in ["0", "0.00", "-5.00", "1.234", "abc", "1e3", ""]:
        r = transfer(client, h, {"from_account_id": accounts["4012345"]["id"],
                                 "to_account_id": accounts["4012346"]["id"], "amount": bad})
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR", bad


def test_memo_limit_and_destination_exclusivity(client, login, accounts):
    h = login("alice")
    base = {"from_account_id": accounts["4012345"]["id"], "amount": "1.00"}
    both = {**base, "to_account_id": accounts["4012346"]["id"], "payee_id": "x"}
    assert transfer(client, h, both).status_code == 422
    assert transfer(client, h, base).status_code == 422
    long = {**base, "to_account_id": accounts["4012346"]["id"], "memo": "x" * 141}
    assert transfer(client, h, long).status_code == 422
    ok = {**base, "to_account_id": accounts["4012346"]["id"], "memo": "x" * 140}
    assert transfer(client, h, ok).status_code == 201


def test_same_account_rejected(client, login, accounts):
    a = accounts["4012345"]["id"]
    assert transfer(client, login("alice"), {"from_account_id": a, "to_account_id": a,
                                             "amount": "1.00"}).status_code == 422


# ---- payees BR-7 / BR-11 ----
def test_payee_rules(client, login, db):
    h = login("alice")
    own = client.post(f"{API}/payees", json={"nickname": "me", "payee_name": "A", "account_number": "4012346"}, headers=h)
    assert own.status_code == 422
    nope = client.post(f"{API}/payees", json={"nickname": "x", "payee_name": "A", "account_number": "9999999"}, headers=h)
    assert nope.status_code == 422
    add_bob_payee(client, h)
    dup = client.post(f"{API}/payees", json={"nickname": "B2", "payee_name": "Bob", "account_number": "4019876"}, headers=h)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "DUPLICATE_PAYEE"
    acts = audit_actions(db)
    assert acts.count("payee_added") == 1


def test_payee_removal_keeps_history(client, login, accounts, db):
    h = login("alice")
    p = add_bob_payee(client, h)
    t = transfer(client, h, {"from_account_id": accounts["4012345"]["id"], "payee_id": p["id"], "amount": "5.00"}).json()
    assert client.delete(f"{API}/payees/{p['id']}", headers=h).status_code == 204
    assert client.get(f"{API}/payees", headers=h).json() == []
    assert client.get(f"{API}/transfers/{t['id']}", headers=h).json()["payee_id"] == p["id"]
    assert "payee_removed" in audit_actions(db)


# ---- BR-8 masking ----
def test_masking(client, login, accounts):
    assert client.get(f"{API}/accounts", headers=login("alice")).json()[0]["account_number"] == "4012345"
    r = client.get(f"{API}/accounts/{accounts['4012345']['id']}", headers=login("sam"))
    assert r.json()["account_number"] == "****2345"


# ---- AC-6 / BR-2 ----
def test_ac6_support_read_only(client, login, accounts, db):
    s = login("sam")
    cust = client.get(f"{API}/accounts/{accounts['4012345']['id']}", headers=s).json()["owner_id"]
    lst = client.get(f"{API}/support/customers/{cust}/accounts", headers=s)
    assert lst.status_code == 200 and len(lst.json()) == 2
    assert client.get(f"{API}/accounts/{accounts['4012345']['id']}/transactions", headers=s).status_code == 200
    body = {"from_account_id": accounts["4012345"]["id"], "to_account_id": accounts["4012346"]["id"], "amount": "1.00"}
    assert transfer(client, s, body).status_code == 403
    assert client.post(f"{API}/payees", json={"nickname": "x", "payee_name": "y", "account_number": "4019876"},
                       headers=s).status_code == 403
    assert client.delete(f"{API}/payees/abc", headers=s).status_code == 403
    assert client.get(f"{API}/accounts", headers=s).status_code == 403
    acts = audit_actions(db)
    assert "support_account_viewed" in acts and "access_denied" in acts


def test_customer_cannot_use_support_endpoint(client, login):
    assert client.get(f"{API}/support/customers/x/accounts", headers=login("alice")).status_code == 403


def test_support_can_read_transfer(client, login, accounts):
    t = transfer(client, login("alice"), {"from_account_id": accounts["4012345"]["id"],
                                          "to_account_id": accounts["4012346"]["id"], "amount": "1.00"}).json()
    assert client.get(f"{API}/transfers/{t['id']}", headers=login("sam")).status_code == 200
    assert client.get(f"{API}/transfers/{t['id']}", headers=login("bob")).status_code == 404


# ---- BR-10 history ----
def test_history_pagination_and_filter(client, login, accounts):
    h = login("alice")
    body = {"from_account_id": accounts["4012345"]["id"], "to_account_id": accounts["4012346"]["id"], "amount": "1.00"}
    for i in range(5):
        assert transfer(client, h, body, key=f"k{i}").status_code == 201
    url = f"{API}/accounts/{accounts['4012345']['id']}/transactions"
    p1 = client.get(url, params={"limit": 2}, headers=h).json()
    assert len(p1["items"]) == 2 and p1["next_cursor"]
    p2 = client.get(url, params={"limit": 2, "cursor": p1["next_cursor"]}, headers=h).json()
    p3 = client.get(url, params={"limit": 2, "cursor": p2["next_cursor"]}, headers=h).json()
    ids = [t["id"] for p in (p1, p2, p3) for t in p["items"]]
    assert len(ids) == 5 == len(set(ids)) and p3["next_cursor"] is None
    assert client.get(url, params={"limit": 101}, headers=h).status_code == 422
    assert client.get(url, params={"from": "2000-01-01T00:00:00Z", "to": "2000-01-02T00:00:00Z"},
                      headers=h).json()["items"] == []
    assert client.get(url, params={"cursor": "garbage"}, headers=h).status_code == 422


# ---- AC-7 ----
def test_ac7_all_audited_actions(client, login, accounts, db):
    h = login("alice")
    client.post(f"{API}/auth/token", json={"username": "alice", "password": "bad"})
    p = add_bob_payee(client, h)
    transfer(client, h, {"from_account_id": accounts["4012345"]["id"], "payee_id": p["id"], "amount": "1.00"})
    transfer(client, h, {"from_account_id": accounts["4012345"]["id"], "payee_id": p["id"], "amount": "999999.00"}, key="k2")
    client.delete(f"{API}/payees/{p['id']}", headers=h)
    client.get(f"{API}/accounts/{accounts['4019876']['id']}", headers=h)
    client.get(f"{API}/accounts/{accounts['4012345']['id']}", headers=login("sam"))
    assert {"login_success", "login_failure", "payee_added", "payee_removed", "transfer_created",
            "transfer_failed", "access_denied", "support_account_viewed"} <= set(audit_actions(db))


def test_error_shape_and_money_format(client, login):
    r = client.get(f"{API}/accounts", headers=login("alice"))
    assert r.json()[0]["balance"] == "5000.00"
    assert client.get(f"{API}/nope").json()["error"]["code"] == "NOT_FOUND"
