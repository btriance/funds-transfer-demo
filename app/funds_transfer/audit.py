import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import AuditEvent

# Defence in depth: never let 7-digit account numbers or bearer tokens reach logs.
_SENSITIVE = [
    (re.compile(r"\b\d{7}\b"), "***"),
    (re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+"), "[token]"),
]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        for pat, repl in _SENSITIVE:
            msg = pat.sub(repl, msg)
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": msg,
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


logger = logging.getLogger("funds_transfer")


def audit(db: Session, *, action: str, outcome: str = "success", actor_id=None, target_type=None,
          target_id=None, source_ip=None, details=None) -> None:
    """Persist an audit event and commit immediately so it survives later rollbacks."""
    db.add(AuditEvent(action=action, outcome=outcome, actor_id=actor_id, target_type=target_type,
                      target_id=target_id, source_ip=source_ip, details=details or {}))
    db.commit()
    logger.info("audit", extra={"fields": {"action": action, "outcome": outcome, "actor_id": actor_id,
                                           "target_type": target_type, "target_id": target_id}})
