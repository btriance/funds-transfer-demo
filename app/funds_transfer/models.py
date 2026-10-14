import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    # SQLite drops tzinfo, so timestamps are stored as naive UTC.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    full_name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16))
    daily_payee_limit_cents: Mapped[int] = mapped_column(BigInteger, default=300_000, server_default="300000")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (CheckConstraint("balance_cents >= 0"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    account_number: Mapped[str] = mapped_column(String(7), unique=True)
    type: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(3), default="CAD")
    balance_cents: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Payee(Base):
    __tablename__ = "payees"
    __table_args__ = (UniqueConstraint("owner_id", "account_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    nickname: Mapped[str] = mapped_column(String(64))
    payee_name: Mapped[str] = mapped_column(String(128))
    account_number: Mapped[str] = mapped_column(String(7))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Transfer(Base):
    __tablename__ = "transfers"
    __table_args__ = (Index("ix_transfer_idem", "created_by", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    from_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    to_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    # No FK: removing a payee must not affect past transfers (BR-11).
    payee_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    memo: Mapped[Optional[str]] = mapped_column(String(140), nullable=True)
    status: Mapped[str] = mapped_column(String(24))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VerificationChallenge(Base):
    __tablename__ = "verification_challenges"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    transfer_id: Mapped[str] = mapped_column(ForeignKey("transfers.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (Index("ix_txn_account_created", "account_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    transfer_id: Mapped[str] = mapped_column(ForeignKey("transfers.id"))
    direction: Mapped[str] = mapped_column(String(8))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    description: Mapped[str] = mapped_column(String(256))
    balance_after_cents: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(String(8))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
