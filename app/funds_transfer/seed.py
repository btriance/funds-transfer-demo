from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Account, User
from .security import hash_password

# Local development passwords only; documented in the README.
SEED_USERS = [
    ("alice", "alice-dev-pw", "Alice Anderson", "alice@example.com", "customer",
     [("4012345", "chequing", 500000), ("4012346", "savings", 2000000)]),
    ("bob", "bob-dev-pw", "Bob Brown", "bob@example.com", "customer",
     [("4019876", "chequing", 250000)]),
    ("sam", "sam-dev-pw", "Sam Support", "sam@example.com", "support_agent", []),
]


def seed(db: Session) -> None:
    if db.scalar(select(User.id).limit(1)):
        return
    for username, pw, name, email, role, accounts in SEED_USERS:
        user = User(username=username, password_hash=hash_password(pw), full_name=name, email=email, role=role)
        db.add(user)
        db.flush()
        for number, typ, cents in accounts:
            db.add(Account(owner_id=user.id, account_number=number, type=typ, balance_cents=cents))
    db.commit()
