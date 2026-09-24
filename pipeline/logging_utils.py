"""One logger per run, writing to the console and to logs/run_<month>_<run_id>.log.

Each run gets its own run_id, so two runs for the same month never share a
log file and an on-call reader can tell them apart.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

FORMAT = "%(asctime)s | %(levelname)-7s | run=%(run_id)s | %(message)s"


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


def get_logger(run_id: str, log_dir: Path | None, label: str, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(f"tlc_pipeline.{run_id}.{label}")
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    formatter = logging.Formatter(FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_dir / f"run_{label}_{run_id}.log", encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(_RunIdFilter(run_id))
        logger.addHandler(handler)
    return logger
