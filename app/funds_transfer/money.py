import re
from decimal import Decimal

from .errors import AppError

_AMOUNT_RE = re.compile(r"^\d{1,10}(\.\d{1,2})?$")


def parse_amount(value: str) -> int:
    """Parse a CAD amount string into cents (BR-3)."""
    if not isinstance(value, str) or not _AMOUNT_RE.match(value.strip()):
        raise AppError(422, "VALIDATION_ERROR", "amount must be a positive number with at most two decimal places")
    cents = int((Decimal(value.strip()) * 100).to_integral_value())
    if cents <= 0:
        raise AppError(422, "VALIDATION_ERROR", "amount must be greater than 0.00")
    return cents


def format_amount(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def mask_account_number(number: str) -> str:
    return "****" + number[-4:]
