import hashlib
import hmac
import logging
import secrets
from typing import Protocol

logger = logging.getLogger("funds_transfer.otp")


class OtpSender(Protocol):
    def send(self, user_id: str, code: str) -> None: ...


class LogOtpSender:
    """Dev-only sender: writes the code to the application log. Replace with the bank's SMS/push service."""

    def send(self, user_id: str, code: str) -> None:
        logger.warning("DEV OTP for user %s: %s", user_id, code)


def generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(secret: str, challenge_id: str, code: str) -> str:
    return hmac.new(secret.encode(), f"{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()


def code_matches(secret: str, challenge_id: str, code: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_code(secret, challenge_id, code), expected_hash)
