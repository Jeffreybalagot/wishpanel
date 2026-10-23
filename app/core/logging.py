"""Logging: human-readable console + structured JSON file, with secret redaction.

Console output is what Unraid's log viewer shows. The JSON file at
/config/logs/wishpanel.log is easy to ship to Loki/Promtail.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
from datetime import UTC, datetime
from pathlib import Path

# Belt-and-braces: we never log secrets on purpose, but scrub anything that
# looks like one in case a third-party library or exception message does.
_REDACTIONS = [
    (
        re.compile(
            r"(?i)(api[_-]?key|apikey|token|password|secret)(\"?\s*[=:]\s*\"?)([^\s\"&,}]+)"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"(?i)(bearer\s+)[a-z0-9._\-]+"), r"\1[REDACTED]"),
]

_STD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "color_message",  # uvicorn duplicate of msg with ANSI codes
}


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            return True
        cleaned = redact(msg)
        if cleaned != msg:
            record.msg, record.args = cleaned, None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extras = {k: v for k, v in vars(record).items() if k not in _STD_ATTRS}
        if extras:
            payload.update({k: _jsonable(v) for k, v in extras.items()})
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {k: v for k, v in vars(record).items() if k not in _STD_ATTRS}
        if extras:
            base += "  " + " ".join(f"{k}={_jsonable(v)}" for k, v in extras.items())
        return base


def _jsonable(value: object) -> object:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def configure_logging(level: str, log_dir: Path | None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    redactor = RedactingFilter()

    console = logging.StreamHandler()
    console.setFormatter(ConsoleFormatter())
    console.addFilter(redactor)
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "wishpanel.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)

    # Keep noisy libraries at a sensible level and route uvicorn through our handlers.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    for name in ("httpx", "httpcore", "aiosqlite", "alembic.runtime.migration"):
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))
