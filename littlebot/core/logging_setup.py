"""Structured logging: readable console output or JSON lines, optional rotating file."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Any

from littlebot.core.config import LoggingConfig

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)) + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "thread": record.threadName,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                payload[k] = v if isinstance(v, (str, int, float, bool, type(None))) else repr(v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)-24s %(message)s", datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")}
        if extras:
            base += "  " + " ".join(f"{k}={v}" for k, v in extras.items())
        return base


class _RedactSecrets(logging.Filter):
    """Belt-and-braces: scrub anything that looks like an API key from log lines."""

    PREFIXES = ("sk-ant-", "sk-proj-", "sk-")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if any(p in msg for p in self.PREFIXES):
            words = []
            for w in msg.split():
                words.append("[REDACTED]" if any(w.strip("'\",:;()").startswith(p) and len(w) > 20 for p in self.PREFIXES) else w)
            record.msg, record.args = " ".join(words), ()
        return True


def setup_logging(cfg: LoggingConfig, data_dir: Path | None = None) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(cfg.level.upper())
    fmt: logging.Formatter = JsonFormatter() if cfg.format == "json" else ConsoleFormatter()
    redact = _RedactSecrets()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    console.addFilter(redact)
    root.addHandler(console)

    if cfg.file:
        path = Path(cfg.file)
        if not path.is_absolute() and data_dir is not None:
            path = data_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=cfg.max_bytes, backupCount=cfg.backup_count, encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        fh.addFilter(redact)
        root.addHandler(fh)

    for noisy in ("httpx", "httpcore", "uvicorn.access", "PIL", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
