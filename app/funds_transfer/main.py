import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional

import jwt as pyjwt
from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .audit import audit, configure_logging
from .config import Settings, get_settings
from .db import Base, make_engine, make_session_factory
from .errors import AppError, not_found
from .models import Account, Payee, Transaction, Transfer, User, VerificationChallenge, utcnow
from .otp import LogOtpSender, OtpSender, code_matches, generate_code, hash_code
from .money import format_amount, mask_account_number, parse_amount
from .schemas import PayeeCreate, TokenRequest, TransferCreate, VerifyRequest, clean_memo
from .security import decode_token, hash_password, issue_token, verify_password
from .seed import seed

bearer = HTTPBearer(auto_error=False)
_DUMMY_HASH = hash_password("dummy-password-for-timing")
TORONTO = ZoneInfo("America/Toronto")
STEP_UP_THRESHOLD_CENTS = 100_000
OTP_TTL = timedelta(minutes=5)
OTP_MAX_ATTEMPTS = 3
COUNTED_STATUSES = ("completed", "pending_verification")


def start_of_today_utc() -> datetime:
    midnight = datetime.now(TORONTO).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None)


def iso(dt: datetime) -> str:
    return dt.isoformat() + "Z"


def create_app(settings: Optional[Settings] = None, otp_sender: Optional[OtpSender] = None) -> FastAPI:
    settings = settings or get_settings()
    otp_sender = otp_sender or LogOtpSender()
    configure_logging()
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as s:
        seed(s)

    app = FastAPI(title="Funds Transfer Service", version="1.0.0")
    app.state.session_factory = session_factory

    def get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    # ---------- error handling ----------
    def err_response(status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return err_response(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        fields = ", ".join(".".join(str(p) for p in e["loc"] if p != "body") for e in exc.errors())
        return err_response(422, "VALIDATION_ERROR", f"Invalid request: {fields}")

    @app.exception_handler(404)
    async def _nf(_: Request, __):
        return err_response(404, "NOT_FOUND", "Resource not found")

    @app.exception_handler(405)
    async def _mna(_: Request, __):
        return err_response(405, "VALIDATION_ERROR", "Method not allowed")

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, __):
        return err_response(500, "INTERNAL_ERROR", "Internal server error")

    # ---------- auth dependencies ----------
    def ip_of(request: Request) -> Optional[str]:
        return request.client.host if request.client else None

    def current_user(request: Request, creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
                     db: Session = Depends(get_db)) -> User:
        if creds is None:
            raise AppError(401, "UNAUTHENTICATED", "Missing bearer token")
        try:
            claims = decode_token(creds.credentials, settings.jwt_secret, settings.jwt_algorithm)
        except pyjwt.PyJWTError:
            raise AppError(401, "UNAUTHENTICATED", "Invalid or expired token")
        user = db.get(User, claims["sub"])
        if user is None:
            raise AppError(401, "UNAUTHENTICATED", "Invalid or expired token")
        return user

    def require_roles(*roles: str):
        def dep(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)) -> User:
            if user.role not in roles:
                audit(db, action="access_denied", outcome="failure", actor_id=user.id, source_ip=ip_of(request),
                      details={"method": request.method, "path": request.url.path, "role": user.role})
                raise AppError(403, "FORBIDDEN", "You do not have permission to perform this action")
            return user
        return dep

    customer_only = require_roles("customer")
    any_user = require_roles("customer", "support_agent")

    def deny_as_not_found(db: Session, user: User, request: Request, target_type: str, target_id: str):
        audit(db, action="access_denied", outcome="failure", actor_id=user.id, target_type=target_type,
              target_id=target_id, source_ip=ip_of(request), details={"reason": "not_owner"})
        return not_found(target_type.capitalize())

    # ---------- serializers ----------
    def account_out(a: Account, reveal: bool) -> dict:
        return {
            "id": a.id,
            "owner_id": a.owner_id,
            "account_number": a.account_number if reveal else mask_account_number(a.account_number),
            "type": a.type,
            "currency": a.currency,
            "balance": format_amount(a.balance_cents),
            "status": a.status,
            "created_at": iso(a.created_at),
        }

    def payee_out(p: Payee) -> dict:
        return {"id": p.id, "nickname": p.nickname, "payee_name": p.payee_name,
                "account_number": mask_account_number(p.account_number), "created_at": iso(p.created_at)}

    def transfer_out(t: Transfer) -> dict:
        return {"id": t.id, "from_account_id": t.from_account_id, "to_account_id": t.to_account_id,
                "payee_id": t.payee_id, "amount": format_amount(t.amount_cents), "memo": t.memo,
                "status": t.status, "created_at": iso(t.created_at)}

    def expire_stale(db: Session, user_id: str) -> None:
        stale = list(db.scalars(select(Transfer).join(
            VerificationChallenge, VerificationChallenge.transfer_id == Transfer.id).where(
            Transfer.created_by == user_id, Transfer.status == "pending_verification",
            VerificationChallenge.expires_at <= utcnow())))
        for t in stale:
            t.status = "expired"
        if stale:
            db.commit()

    def used_today_cents(db: Session, user_id: str, exclude_id: Optional[str] = None) -> int:
        q = select(func.coalesce(func.sum(Transfer.amount_cents), 0)).where(
            Transfer.created_by == user_id, Transfer.payee_id.is_not(None),
            Transfer.status.in_(COUNTED_STATUSES), Transfer.created_at >= start_of_today_utc())
        if exclude_id:
            q = q.where(Transfer.id != exclude_id)
        return int(db.scalar(q))

    def settle(db: Session, transfer: Transfer, src: Account, dst: Account, cents: int) -> bool:
        """Move funds atomically; returns False (nothing changed) on insufficient funds."""
        # Conditional update guarantees no overdraft even under concurrent requests.
        debited = db.execute(update(Account).where(
            Account.id == src.id, Account.status == "open", Account.balance_cents >= cents,
        ).values(balance_cents=Account.balance_cents - cents))
        if debited.rowcount != 1:
            return False
        db.execute(update(Account).where(Account.id == dst.id).values(balance_cents=Account.balance_cents + cents))
        db.flush()
        src_bal = db.scalar(select(Account.balance_cents).where(Account.id == src.id))
        dst_bal = db.scalar(select(Account.balance_cents).where(Account.id == dst.id))
        now = utcnow()
        db.add_all([
            Transaction(account_id=src.id, transfer_id=transfer.id, direction="debit", amount_cents=cents,
                        description=f"Transfer to {mask_account_number(dst.account_number)}",
                        balance_after_cents=src_bal, created_at=now),
            Transaction(account_id=dst.id, transfer_id=transfer.id, direction="credit", amount_cents=cents,
                        description=f"Transfer from {mask_account_number(src.account_number)}",
                        balance_after_cents=dst_bal, created_at=now),
        ])
        transfer.status = "completed"
        return True

    def txn_out(t: Transaction) -> dict:
        return {"id": t.id, "account_id": t.account_id, "transfer_id": t.transfer_id, "direction": t.direction,
                "amount": format_amount(t.amount_cents), "description": t.description,
                "balance_after": format_amount(t.balance_after_cents), "created_at": iso(t.created_at)}

    api = APIRouter(prefix="/api/v1")

    # ---------- public ----------
    @api.get("/health", tags=["system"])
    def health():
        return {"status": "ok"}

    @api.post("/auth/token", tags=["auth"])
    def token(body: TokenRequest, request: Request, db: Session = Depends(get_db)):
        user = db.scalar(select(User).where(User.username == body.username))
        ok = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)
        if not user or not ok:
            audit(db, action="login_failure", outcome="failure", source_ip=ip_of(request),
                  details={"username": body.username[:64]})
            raise AppError(401, "UNAUTHENTICATED", "Invalid username or password")
        audit(db, action="login_success", actor_id=user.id, target_type="user", target_id=user.id,
              source_ip=ip_of(request))
        tok = issue_token(user.id, user.role, settings.jwt_secret, settings.jwt_algorithm,
                          settings.token_ttl_minutes)
        return {"access_token": tok, "token_type": "bearer", "expires_in": settings.token_ttl_minutes * 60}

    # ---------- accounts ----------
    @api.get("/accounts", tags=["accounts"])
    def list_accounts(user: User = Depends(customer_only), db: Session = Depends(get_db)):
        rows = db.scalars(select(Account).where(Account.owner_id == user.id).order_by(Account.account_number))
        return [account_out(a, reveal=True) for a in rows]

    def load_account_for(db: Session, user: User, request: Request, account_id: str) -> Account:
        acct = db.get(Account, account_id)
        if acct is None:
            raise not_found("Account")
        if user.role == "customer" and acct.owner_id != user.id:
            raise deny_as_not_found(db, user, request, "account", account_id)
        if user.role == "support_agent":
            audit(db, action="support_account_viewed", actor_id=user.id, target_type="account",
                  target_id=account_id, source_ip=ip_of(request))
        return acct

    @api.get("/accounts/{account_id}", tags=["accounts"])
    def get_account(account_id: str, request: Request, user: User = Depends(any_user),
                    db: Session = Depends(get_db)):
        acct = load_account_for(db, user, request, account_id)
        return account_out(acct, reveal=acct.owner_id == user.id)

    @api.get("/accounts/{account_id}/transactions", tags=["accounts"])
    def list_transactions(
        account_id: str, request: Request,
        from_: Optional[datetime] = Query(None, alias="from"),
        to: Optional[datetime] = None,
        limit: int = Query(50, ge=1, le=100),
        cursor: Optional[str] = None,
        user: User = Depends(any_user), db: Session = Depends(get_db),
    ):
        acct = load_account_for(db, user, request, account_id)

        def naive_utc(dt: datetime) -> datetime:
            return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt

        end = naive_utc(to) if to else utcnow()
        start = naive_utc(from_) if from_ else end - timedelta(days=30)
        q = select(Transaction).where(Transaction.account_id == acct.id,
                                      Transaction.created_at >= start, Transaction.created_at <= end)
        if cursor:
            try:
                ts, tid = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
                c_ts = datetime.fromisoformat(ts)
            except Exception:
                raise AppError(422, "VALIDATION_ERROR", "Invalid cursor")
            q = q.where((Transaction.created_at < c_ts) | ((Transaction.created_at == c_ts) & (Transaction.id < tid)))
        q = q.order_by(Transaction.created_at.desc(), Transaction.id.desc()).limit(limit + 1)
        rows = list(db.scalars(q))
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = base64.urlsafe_b64encode(f"{last.created_at.isoformat()}|{last.id}".encode()).decode()
        return {"items": [txn_out(t) for t in rows], "next_cursor": next_cursor}

    @api.get("/support/customers/{customer_id}/accounts", tags=["support"])
    def support_customer_accounts(customer_id: str, request: Request,
                                  user: User = Depends(require_roles("support_agent")),
                                  db: Session = Depends(get_db)):
        cust = db.get(User, customer_id)
        if cust is None or cust.role != "customer":
            raise not_found("Customer")
        audit(db, action="support_account_viewed", actor_id=user.id, target_type="user", target_id=customer_id,
              source_ip=ip_of(request))
        rows = db.scalars(select(Account).where(Account.owner_id == customer_id).order_by(Account.account_number))
        return [account_out(a, reveal=False) for a in rows]

    # ---------- payees ----------
    @api.get("/payees", tags=["payees"])
    def list_payees(user: User = Depends(customer_only), db: Session = Depends(get_db)):
        rows = db.scalars(select(Payee).where(Payee.owner_id == user.id).order_by(Payee.created_at))
        return [payee_out(p) for p in rows]

    @api.post("/payees", status_code=201, tags=["payees"])
    def add_payee(body: PayeeCreate, request: Request, user: User = Depends(customer_only),
                  db: Session = Depends(get_db)):
        target = db.scalar(select(Account).where(Account.account_number == body.account_number))
        if target is None or target.status != "open":
            raise AppError(422, "VALIDATION_ERROR", "account_number must belong to an open account at this bank")
        if target.owner_id == user.id:
            raise AppError(422, "VALIDATION_ERROR", "You cannot add your own account as a payee")
        dup = db.scalar(select(Payee.id).where(Payee.owner_id == user.id,
                                               Payee.account_number == body.account_number))
        if dup:
            raise AppError(409, "DUPLICATE_PAYEE", "This payee already exists")
        payee = Payee(owner_id=user.id, nickname=body.nickname.strip(), payee_name=body.payee_name.strip(),
                      account_number=body.account_number)
        db.add(payee)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise AppError(409, "DUPLICATE_PAYEE", "This payee already exists")
        audit(db, action="payee_added", actor_id=user.id, target_type="payee", target_id=payee.id,
              source_ip=ip_of(request))
        return payee_out(payee)

    @api.delete("/payees/{payee_id}", status_code=204, tags=["payees"])
    def remove_payee(payee_id: str, request: Request, user: User = Depends(customer_only),
                     db: Session = Depends(get_db)):
        payee = db.get(Payee, payee_id)
        if payee is None:
            raise not_found("Payee")
        if payee.owner_id != user.id:
            raise deny_as_not_found(db, user, request, "payee", payee_id)
        db.delete(payee)
        db.commit()
        audit(db, action="payee_removed", actor_id=user.id, target_type="payee", target_id=payee_id,
              source_ip=ip_of(request))
        return Response(status_code=204)

    # ---------- transfers ----------
    @api.post("/transfers", status_code=201, tags=["transfers"])
    def create_transfer(
        body: TransferCreate, request: Request, response: Response,
        idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
        user: User = Depends(customer_only), db: Session = Depends(get_db),
    ):
        if not idempotency_key or len(idempotency_key) > 128:
            raise AppError(422, "VALIDATION_ERROR", "Idempotency-Key header is required (max 128 characters)")
        cents = parse_amount(body.amount)
        memo = clean_memo(body.memo)
        ip = ip_of(request)

        src = db.get(Account, body.from_account_id)
        if src is None or src.owner_id != user.id:
            if src is not None:
                raise deny_as_not_found(db, user, request, "account", body.from_account_id)
            raise not_found("Account")

        payee = None
        if body.payee_id is not None:
            payee = db.get(Payee, body.payee_id)
            if payee is None:
                raise not_found("Payee")
            if payee.owner_id != user.id:
                raise deny_as_not_found(db, user, request, "payee", body.payee_id)
            dst = db.scalar(select(Account).where(Account.account_number == payee.account_number))
            if dst is None:
                raise AppError(422, "ACCOUNT_NOT_OPEN", "Destination account is not open")
        else:
            dst = db.get(Account, body.to_account_id)
            if dst is None:
                raise not_found("Account")
            if dst.owner_id != user.id:
                raise deny_as_not_found(db, user, request, "account", body.to_account_id)
        if dst.id == src.id:
            raise AppError(422, "VALIDATION_ERROR", "Source and destination accounts must differ")

        req_hash = hashlib.sha256(json.dumps(
            [src.id, dst.id, payee.id if payee else None, cents, memo]).encode()).hexdigest()
        window = utcnow() - timedelta(hours=24)
        prior = db.scalar(select(Transfer).where(
            Transfer.created_by == user.id, Transfer.idempotency_key == idempotency_key,
            Transfer.created_at >= window).order_by(Transfer.created_at.desc()))
        if prior is not None:
            if prior.request_hash != req_hash:
                raise AppError(409, "IDEMPOTENCY_CONFLICT", "Idempotency-Key was used with a different request")
            response.status_code = 200
            return transfer_out(prior)

        def fail(code: str, message: str, status: int = 422):
            db.rollback()
            audit(db, action="transfer_failed", outcome="failure", actor_id=user.id, target_type="account",
                  target_id=src.id, source_ip=ip, details={"code": code, "amount": format_amount(cents)})
            return AppError(status, code, message)

        if src.status != "open" or dst.status != "open":
            raise fail("ACCOUNT_NOT_OPEN", "Source and destination accounts must be open")

        if payee is not None:
            expire_stale(db, user.id)
            limit = user.daily_payee_limit_cents
            used = used_today_cents(db, user.id)
            if used + cents > limit:
                db.rollback()
                audit(db, action="transfer_limit_exceeded", outcome="failure", actor_id=user.id,
                      target_type="account", target_id=src.id, source_ip=ip,
                      details={"amount": format_amount(cents), "limit": format_amount(limit),
                               "used_today": format_amount(used)})
                raise AppError(422, "DAILY_LIMIT_EXCEEDED", "Daily payee transfer limit exceeded")

        needs_step_up = payee is not None and cents >= STEP_UP_THRESHOLD_CENTS
        transfer = Transfer(from_account_id=src.id, to_account_id=dst.id, payee_id=payee.id if payee else None,
                            amount_cents=cents, memo=memo, status="pending_verification" if needs_step_up else "completed",
                            idempotency_key=idempotency_key, request_hash=req_hash, created_by=user.id)
        db.add(transfer)
        db.flush()

        if needs_step_up:
            if src.balance_cents < cents:
                raise fail("INSUFFICIENT_FUNDS", "Insufficient funds")
            challenge = VerificationChallenge(transfer_id=transfer.id, code_hash="",
                                              expires_at=utcnow() + OTP_TTL)
            db.add(challenge)
            db.flush()
            code = generate_code()
            challenge.code_hash = hash_code(settings.jwt_secret, challenge.id, code)
            db.commit()
            otp_sender.send(user.id, code)
            audit(db, action="step_up_challenge_issued", actor_id=user.id, target_type="transfer",
                  target_id=transfer.id, source_ip=ip, details={"amount": format_amount(cents)})
            response.status_code = 202
            out = transfer_out(transfer)
            out["verification"] = {"challenge_id": challenge.id, "expires_at": iso(challenge.expires_at)}
            return out

        if not settle(db, transfer, src, dst, cents):
            raise fail("INSUFFICIENT_FUNDS", "Insufficient funds")
        db.commit()
        audit(db, action="transfer_created", actor_id=user.id, target_type="transfer", target_id=transfer.id,
              source_ip=ip, details={"amount": format_amount(cents)})
        return transfer_out(transfer)

    @api.post("/transfers/{transfer_id}/verify", tags=["transfers"])
    def verify_transfer(transfer_id: str, body: VerifyRequest, request: Request,
                        user: User = Depends(customer_only), db: Session = Depends(get_db)):
        ip = ip_of(request)
        t = db.get(Transfer, transfer_id)
        if t is None:
            raise not_found("Transfer")
        if t.created_by != user.id:
            raise deny_as_not_found(db, user, request, "transfer", transfer_id)
        expire_stale(db, user.id)
        db.refresh(t)
        if t.status == "expired":
            raise AppError(410, "VERIFICATION_EXPIRED", "Verification code has expired")
        if t.status != "pending_verification":
            raise AppError(409, "TRANSFER_NOT_PENDING", "Transfer is not awaiting verification")
        ch = db.scalar(select(VerificationChallenge).where(VerificationChallenge.transfer_id == t.id))
        if ch is None or ch.consumed_at is not None:
            raise AppError(409, "TRANSFER_NOT_PENDING", "Transfer is not awaiting verification")

        if not code_matches(settings.jwt_secret, ch.id, body.code, ch.code_hash):
            ch.attempts += 1
            cancelled = ch.attempts >= OTP_MAX_ATTEMPTS
            if cancelled:
                t.status = "cancelled"
            db.commit()
            audit(db, action="step_up_failed", outcome="failure", actor_id=user.id, target_type="transfer",
                  target_id=t.id, source_ip=ip, details={"attempts": ch.attempts, "cancelled": cancelled})
            raise AppError(422, "VERIFICATION_FAILED", "Incorrect verification code")

        # Single-use: only one request can flip consumed_at from NULL.
        used = db.execute(update(VerificationChallenge).where(
            VerificationChallenge.id == ch.id, VerificationChallenge.consumed_at.is_(None)
        ).values(consumed_at=utcnow()))
        if used.rowcount != 1:
            db.rollback()
            raise AppError(409, "TRANSFER_NOT_PENDING", "Transfer is not awaiting verification")

        def cancel(code: str, message: str):
            t.status = "cancelled"
            db.commit()
            audit(db, action="step_up_failed", outcome="failure", actor_id=user.id, target_type="transfer",
                  target_id=t.id, source_ip=ip, details={"code": code})
            return AppError(422, code, message)

        if used_today_cents(db, user.id, exclude_id=t.id) + t.amount_cents > user.daily_payee_limit_cents:
            raise cancel("DAILY_LIMIT_EXCEEDED", "Daily payee transfer limit exceeded")
        src = db.get(Account, t.from_account_id)
        dst = db.get(Account, t.to_account_id)
        if src.status != "open" or dst.status != "open":
            raise cancel("ACCOUNT_NOT_OPEN", "Source and destination accounts must be open")
        if not settle(db, t, src, dst, t.amount_cents):
            raise cancel("INSUFFICIENT_FUNDS", "Insufficient funds")
        db.commit()
        audit(db, action="step_up_verified", actor_id=user.id, target_type="transfer", target_id=t.id,
              source_ip=ip)
        audit(db, action="transfer_created", actor_id=user.id, target_type="transfer", target_id=t.id,
              source_ip=ip, details={"amount": format_amount(t.amount_cents)})
        return transfer_out(t)

    @api.get("/limits", tags=["transfers"])
    def get_limits(user: User = Depends(customer_only), db: Session = Depends(get_db)):
        expire_stale(db, user.id)
        used = used_today_cents(db, user.id)
        limit = user.daily_payee_limit_cents
        return {"daily_payee_limit": format_amount(limit), "used_today": format_amount(used),
                "remaining_today": format_amount(max(limit - used, 0)),
                "step_up_threshold": format_amount(STEP_UP_THRESHOLD_CENTS)}

    @api.get("/transfers/{transfer_id}", tags=["transfers"])
    def get_transfer(transfer_id: str, request: Request, user: User = Depends(any_user),
                     db: Session = Depends(get_db)):
        t = db.get(Transfer, transfer_id)
        if t is None:
            raise not_found("Transfer")
        if user.role == "customer" and t.created_by != user.id:
            raise deny_as_not_found(db, user, request, "transfer", transfer_id)
        if user.role == "support_agent":
            audit(db, action="support_account_viewed", actor_id=user.id, target_type="transfer",
                  target_id=transfer_id, source_ip=ip_of(request))
        return transfer_out(t)

    app.include_router(api)
    return app
