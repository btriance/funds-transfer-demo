import re
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class TokenRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PayeeCreate(BaseModel):
    nickname: str = Field(min_length=1, max_length=64)
    payee_name: str = Field(min_length=1, max_length=128)
    account_number: str = Field(pattern=r"^\d{7}$")


class TransferCreate(BaseModel):
    from_account_id: str
    to_account_id: Optional[str] = None
    payee_id: Optional[str] = None
    amount: str
    memo: Optional[str] = Field(default=None, max_length=140)

    @model_validator(mode="after")
    def _exactly_one_destination(self):
        if (self.to_account_id is None) == (self.payee_id is None):
            raise ValueError("exactly one of to_account_id or payee_id must be provided")
        return self


_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_memo(memo: Optional[str]) -> Optional[str]:
    return _CTRL.sub("", memo) if memo is not None else None
