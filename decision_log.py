"""Append-only decision log: one entry per agent action.

Every entry has ``timestamp``, ``agent``, ``action``, ``sources``, ``model``,
``cost`` and ``approved_by``, plus any extras (``outcome``, ``phase``, ``step``,
``run_id``, ``status``, ``tokens``...). Entries are validated before they are
written, and the file is replaced atomically so a crash never leaves half a log.

Default location: ``runs/<run_id>/decision_log.json`` (``runs/`` is git-ignored).
Only a ``--demo`` run writes to the root ``decision_log.json``.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs"
DEMO_LOG_PATH = ROOT / "decision_log.json"
REQUIRED_KEYS = ("timestamp", "agent", "action", "sources", "model", "cost", "approved_by")


class LogValidationError(ValueError):
    """An entry (or the log file itself) does not have the required shape."""


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_http_url(value) -> bool:
    if not isinstance(value, str) or not value.strip() or any(c.isspace() for c in value):
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def default_log_path(run_id: str, runs_dir: Path | None = None) -> Path:
    return Path(runs_dir or RUNS_DIR) / run_id / "decision_log.json"


def validate_entry(entry: dict) -> None:
    for key in REQUIRED_KEYS:
        if key not in entry:
            raise LogValidationError(f"log entry is missing the key '{key}'")
    for key in ("agent", "action"):
        if not isinstance(entry[key], str) or not entry[key].strip():
            raise LogValidationError(f"'{key}' must be a non-empty string")
    if not isinstance(entry["timestamp"], str) or not entry["timestamp"].endswith("Z"):
        raise LogValidationError("'timestamp' must be a UTC ISO 8601 string ending in Z")
    sources = entry["sources"]
    if not isinstance(sources, list):
        raise LogValidationError("'sources' must be a list of http(s) URLs")
    for url in sources:
        if not is_http_url(url):
            raise LogValidationError(f"source is not an http(s) URL: {url!r}")
    if entry["model"] is not None and not isinstance(entry["model"], str):
        raise LogValidationError("'model' must be a string or None")
    cost = entry["cost"]
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        raise LogValidationError(f"'cost' must be a non-negative number in USD, got {cost!r}")
    if entry["approved_by"] not in ("founder", None):
        raise LogValidationError("'approved_by' must be \"founder\" or None")


class DecisionLogger:
    """Appends validated entries to one JSON file."""

    def __init__(self, path, run_id: str | None = None, reset: bool = False):
        self.path = Path(path)
        self.run_id = run_id
        if reset or not self.path.exists():
            self._write([])

    def entries(self) -> list:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "[]")
        except FileNotFoundError:
            return []
        except json.JSONDecodeError as e:
            raise LogValidationError(f"{self.path} is not valid JSON: {e}") from e
        if not isinstance(data, list):
            raise LogValidationError(f"{self.path} must hold a JSON list")
        return data

    def log(self, agent, action, sources=None, model=None, cost=0, approved_by=None, **extra) -> dict:
        entry = {
            "timestamp": utc_timestamp(),
            "agent": agent,
            "action": action,
            "sources": list(sources) if sources is not None else [],
            "model": model,
            "cost": cost,
            "approved_by": approved_by,
        }
        for key in extra:
            if key in entry:
                raise LogValidationError(f"extra key '{key}' would overwrite a required key")
        if self.run_id is not None and "run_id" not in extra:
            entry["run_id"] = self.run_id
        entry.update(extra)
        validate_entry(entry)
        try:
            entry = json.loads(json.dumps(entry, ensure_ascii=False, default=str))
        except (TypeError, ValueError) as e:
            raise LogValidationError(f"log entry is not JSON serialisable: {e}") from e
        data = self.entries()
        data.append(entry)
        self._write(data)
        return entry

    def _write(self, data: list) -> None:
        """Write to a temp file in the same folder, then replace: all or nothing."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".decision_log_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(5):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:  # Windows: another process is briefly reading the file
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


_default: DecisionLogger | None = None


def configure(path=None, run_id: str | None = None, reset: bool = False) -> DecisionLogger:
    """Set the logger used by the module-level ``log``. Default: runs/<run_id>/decision_log.json."""
    global _default
    run_id = run_id or new_run_id()
    _default = DecisionLogger(path or default_log_path(run_id), run_id=run_id, reset=reset)
    return _default


def log(agent, action, sources=None, model=None, cost=0, approved_by=None, **extra) -> dict:
    """Append one entry with the default logger (created under runs/ on first use)."""
    logger = _default or configure()
    return logger.log(agent, action, sources=sources, model=model, cost=cost, approved_by=approved_by, **extra)
