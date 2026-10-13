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
import os
import sys
import time
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


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotation that never loses records or old logs when the file can't be renamed.

    On Windows another program reading the log (``homemesh logs -f`` from an older version, an
    editor, ``Get-Content -Wait``) blocks renaming it. The standard handler first shifts the old
    backups (deleting the oldest) and then fails, on every record. Here the live file is moved
    first; if that fails, writing simply continues in the current file and rotation is retried
    a minute later.
    """

    retry_after_s = 60.0

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._blocked_until = 0.0

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if time.monotonic() < self._blocked_until:
            return False
        return bool(super().shouldRollover(record))

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]
        base = self.baseFilename
        moving = base + ".rotating"
        try:
            os.replace(base, moving)
        except OSError:
            self._blocked_until = time.monotonic() + self.retry_after_s
            self.stream = self._open()
            return
        if self.backupCount > 0:
            for i in range(self.backupCount - 1, 0, -1):
                src, dst = f"{base}.{i}", f"{base}.{i + 1}"
                if os.path.exists(src):
                    try:
                        os.replace(src, dst)
                    except OSError:
                        pass
            try:
                os.replace(moving, f"{base}.1")
            except OSError:
                pass
        else:
            try:
                os.remove(moving)
            except OSError:
                pass
        self.stream = self._open()


def setup_logging(level: str = "INFO", fmt: str = "text", log_dir: Path | None = None,
                  to_file: bool = True, console: bool = True) -> None:
    global _CONFIGURED
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_homemesh", False):
            root.removeHandler(h)
    root.setLevel(level)
    formatter: logging.Formatter = JsonFormatter() if fmt == "json" else TextFormatter()
    if console and sys.stderr is not None:
        con = logging.StreamHandler(sys.stderr)
        con.setFormatter(formatter)
        con._homemesh = True  # type: ignore[attr-defined]
        root.addHandler(con)
    if to_file and log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = SafeRotatingFileHandler(
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
