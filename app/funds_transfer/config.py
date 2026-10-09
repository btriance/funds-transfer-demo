import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    jwt_secret: str
    database_url: str
    token_ttl_minutes: int
    jwt_algorithm: str = "HS256"


def get_settings() -> Settings:
    secret = os.environ.get("JWT_SECRET")
    if not secret or len(secret) < 32:
        raise RuntimeError("JWT_SECRET environment variable must be set (at least 32 characters)")
    return Settings(
        jwt_secret=secret,
        database_url=os.environ.get("DATABASE_URL", "sqlite:///./funds_transfer.db"),
        token_ttl_minutes=int(os.environ.get("TOKEN_TTL_MINUTES", "15")),
    )
