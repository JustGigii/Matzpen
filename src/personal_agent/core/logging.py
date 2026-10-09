import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "openwa_webhook_secret",
        "password",
        "secret",
        "telegram_bot_token",
        "token",
    }
)
REDACTED = "[REDACTED]"
SENSITIVE_URL_LOGGERS = ("httpx", "httpcore")


def redact(value: Any, key: str | None = None) -> Any:
    """Recursively redact values whose keys commonly contain credentials."""
    if key is not None and any(part in key.lower() for part in SENSITIVE_KEYS):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(item_key): redact(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = getattr(record, "context", None)
        if isinstance(context, Mapping):
            payload["context"] = redact(context)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str, *, json_logs: bool) -> None:
    handler = logging.StreamHandler()
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
    # httpx includes full request URLs in INFO records. Telegram embeds the bot token in its API
    # path, so these transport loggers must never emit request URLs in normal application logs.
    for logger_name in SENSITIVE_URL_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
