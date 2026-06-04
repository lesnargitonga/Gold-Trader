"""Structured JSON logging for the gold-trader stack.

Goals
-----
* Single source of truth for log configuration: any module gets its logger
  via ``get_logger(__name__)``; configuration is set up once at process start
  via ``configure_logging(...)``.
* Machine-readable: every log line is one JSON object with stable field
  names — ``ts`` (UTC ISO-8601), ``level``, ``logger``, ``event``, ``msg``,
  plus any structured ``extra={...}`` fields.
* Durable: rotating file handler under ``logs/`` (default 10 MB × 5 files);
  console mirror for human inspection.
* Cron-friendly: when stderr is a tty, console output is plain text; when
  redirected to a file (cron), console output is JSON too so log shippers
  can parse it.

Usage
-----
    from gold_trader.infra import configure_logging, get_logger
    configure_logging(log_dir=Path("logs"), level="INFO")
    log = get_logger(__name__)
    log.info("agent_cycle_start", extra={"broker": "mt5_remote"})
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_FIELDS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON object with stable fields."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        # Anything passed via logger.info("msg", extra={...}) lands as
        # attributes on the record.  Promote those to top-level fields.
        for key, value in record.__dict__.items():
            if key in _DEFAULT_FIELDS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), default=str)


class _PlainFormatter(logging.Formatter):
    """Human-friendly console formatter used when stderr is a tty."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        extras = []
        for key, value in record.__dict__.items():
            if key in _DEFAULT_FIELDS or key.startswith("_"):
                continue
            extras.append(f"{key}={value}")
        suffix = (" " + " ".join(extras)) if extras else ""
        return f"{ts} {record.levelname:<5} {record.name} {record.getMessage()}{suffix}"


_CONFIGURED = False


def configure_logging(
    log_dir: Path | str = Path("logs"),
    level: str | int = "INFO",
    *,
    rotate_max_bytes: int = 10 * 1024 * 1024,
    rotate_backup_count: int = 5,
    console: bool = True,
    json_console: bool | None = None,
    log_filename: str = "gold_trader.jsonl",
) -> Path:
    """Configure the root logger. Idempotent: safe to call repeatedly.

    Returns the path to the rotating log file.
    """
    global _CONFIGURED
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / log_filename

    root = logging.getLogger()
    root.setLevel(level)

    # Wipe existing handlers if we set them up previously, but leave foreign
    # handlers (e.g. pytest capture) alone.
    if _CONFIGURED:
        for handler in list(root.handlers):
            if getattr(handler, "_gold_trader", False):
                root.removeHandler(handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=rotate_max_bytes,
        backupCount=rotate_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())
    file_handler._gold_trader = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    if console:
        if json_console is None:
            # Auto: JSON when not a tty (cron/redirected), plain when tty.
            json_console = not sys.stderr.isatty()
        console_handler = logging.StreamHandler(stream=sys.stderr)
        console_handler.setFormatter(JsonFormatter() if json_console else _PlainFormatter())
        console_handler._gold_trader = True  # type: ignore[attr-defined]
        root.addHandler(console_handler)

    _CONFIGURED = True
    return log_path


def get_logger(name: str) -> logging.Logger:
    """Get a logger; configures defaults if the user forgot to call configure."""
    if not _CONFIGURED:
        # Best-effort default so library use without explicit setup still
        # produces structured logs (under ``logs/`` relative to cwd).
        try:
            configure_logging()
        except OSError:
            # Read-only fs etc. — fall back to bare console.
            logging.basicConfig(level=os.environ.get("GOLD_LOG_LEVEL", "INFO"))
    return logging.getLogger(name)
