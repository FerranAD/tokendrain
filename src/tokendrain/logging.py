"""Structured logs with explicit context and conservative secret handling."""

from __future__ import annotations

import json
import logging
import re
import traceback
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from tokendrain.credentials.store import SecretRedactor

_URL = re.compile(r"https?://[^\s\"<>]+")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;\"']+")
_CONTEXT = (
    "run_id",
    "project_id",
    "execution_id",
    "request_id",
    "schedule_id",
    "account_id",
    "error_type",
    "forced",
    "status",
    "operation",
    "reason",
    "duration_ms",
)


def sanitize(message: str, redactor: SecretRedactor) -> str:
    def safe_url(match: re.Match[str]) -> str:
        try:
            url = urlsplit(match.group())
            return urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
        except ValueError:
            return "[REDACTED URL]"

    return redactor.redact(_BEARER.sub("Bearer [REDACTED]", _URL.sub(safe_url, message)))


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: SecretRedactor | None = None) -> None:
        super().__init__()
        self.redactor = redactor or SecretRedactor()

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        # Uvicorn sometimes supplies a preformatted traceback as the message.
        # Its final exception line may contain credential-bearing values.
        formatted_traceback = "Traceback (most recent call last):" in message
        item: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "event": "exception traceback"
            if formatted_traceback
            else sanitize(message, self.redactor),
            "logger": record.name,
        }
        for name in _CONTEXT:
            value = record.__dict__.get(name)
            if isinstance(value, str):
                item[name] = sanitize(value, self.redactor)
            elif isinstance(value, (int, float, bool)):
                item[name] = value
        if record.exc_info and record.exc_info[0]:
            # Exception strings can contain response bodies or credentials.
            # Preserve type and call sites without values or local variables.
            item["exception"] = {
                "type": record.exc_info[0].__name__,
                "frames": [
                    {
                        "file": Path(frame.filename).name,
                        "line": frame.lineno,
                        "function": frame.name,
                    }
                    for frame in traceback.extract_tb(record.exc_info[2])
                ],
            }
        elif formatted_traceback:
            item["exception"] = {
                "frames": [
                    {"file": Path(filename).name, "line": int(line), "function": name}
                    for filename, line, name in re.findall(
                        r'File "([^"]+)", line (\d+), in ([\w<>]+)', message
                    )
                ],
            }
        return json.dumps(item, ensure_ascii=True)


def configure_logging(level: str = "INFO", redactor: SecretRedactor | None = None) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(redactor))
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
    for name in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
