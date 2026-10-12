"""Structured logging setup.

Text logs for humans on the console; optional JSON lines for machine processing.
A rotating log file under ``<data>/logs`` is written by default so that
``homemesh logs`` works even without systemd/journald (e.g. on Windows).
Secrets (tokens, passwords, node secrets) are never passed to loggers.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict) and extra:
            base += " " + " ".join(f"{k}={v}" for k, v in extra.items())
        return base


def setup_logging(level: str = "INFO", fmt: str = "text", log_dir: Path | None = None,
                  to_file: bool = True) -> None:
    global _CONFIGURED
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_homemesh", False):
            root.removeHandler(h)
    root.setLevel(level)
    formatter: logging.Formatter = JsonFormatter() if fmt == "json" else TextFormatter()
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console._homemesh = True  # type: ignore[attr-defined]
    root.addHandler(console)
    if to_file and log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_dir / "homemesh.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
        fh._homemesh = True  # type: ignore[attr-defined]
        root.addHandler(fh)
    # Quiet noisy libraries; their warnings still surface.
    for name in ("httpx", "httpcore", "websockets", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_fields(logger: logging.Logger, level: int, msg: str, **fields: object) -> None:
    logger.log(level, msg, extra={"fields": fields})
