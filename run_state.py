"""Run state: everything needed to show or resume a run, saved after every change.

The state of run ``<run_id>`` lives in ``runs/<run_id>/state.json`` (``runs/`` is
git-ignored and a Docker volume). Every change is written atomically (temp file
in the same folder, then replace), so a crash or a browser refresh never leaves
half a file, and a restart that loads the file shows the same pending approval.

The orchestrator calls ``save()`` (through the mutators below) after every step,
gate and approval; the UI calls ``load(run_id)``. A ``RunState`` is also the
persisted store for ``approval.PendingApprover``
(``get_pending`` / ``set_pending`` / ``clear_pending`` / ``record_decision``).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from approval import PendingApproval

ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs"
STATE_FILE = "state.json"
STATUSES = ("running", "awaiting_approval", "stopped", "finished")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")


class RunStateError(ValueError):
    """The run id is invalid, or the state file is missing or unreadable."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id) or ".." in run_id:
        raise RunStateError(f"not a valid run id: {run_id!r}")
    return run_id


def runs_root(runs_dir=None) -> Path:
    return Path(runs_dir) if runs_dir is not None else RUNS_DIR


def state_path(run_id: str, runs_dir=None) -> Path:
    return runs_root(runs_dir) / _check_run_id(run_id) / STATE_FILE


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:  # Windows: a reader holds the file for a moment
                if attempt == 9:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class RunState:
    """One run's state. Mutators save immediately; all of them are thread-safe."""

    FIELDS = ("run_id", "status", "brief", "options", "steps", "gates", "pending", "decisions",
              "files", "tokens", "cost", "final_outcome", "stop_reasons", "message",
              "created_at", "updated_at", "started_at", "finished_at", "wall_seconds", "output_dir", "log_path",
              "cursor")

    def __init__(self, run_id: str, brief: dict | None = None, options: dict | None = None, runs_dir=None):
        self.run_id = _check_run_id(run_id)
        self.runs_dir = runs_root(runs_dir)
        self.status = "running"
        self.brief = dict(brief or {})
        self.options = dict(options or {})
        self.steps: list = []        # {"id", "phase", "step", "name", "agent", "status", "attempt", "file", "tokens", "seconds"}
        self.gates: list = []        # {"after", "gate", "system_outcome", "founder_outcome", "outcome", "target", "actual", ...}
        self.pending: PendingApproval | None = None
        self.decisions: list = []    # every founder decision, as PendingApproval dicts
        self.files: list = []
        self.tokens = 0
        self.cost = 0.0
        self.final_outcome: str | None = None
        self.stop_reasons: list = []
        self.message = ""
        self.created_at = self.updated_at = _now()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.wall_seconds: float | None = None
        self.output_dir: str | None = None
        self.log_path: str | None = None
        # Where the orchestrator is (step index, attempts, pivots, the gate decision being reviewed,
        # serialised step results...). Opaque to the UI; owned by the orchestrator.
        self.cursor: dict = {}
        self._lock = threading.RLock()

    # --- persistence -----------------------------------------------------
    @property
    def path(self) -> Path:
        return state_path(self.run_id, self.runs_dir)

    @property
    def run_dir(self) -> Path:
        return self.path.parent

    def to_dict(self) -> dict:
        with self._lock:
            data = {k: getattr(self, k) for k in self.FIELDS}
            data["pending"] = self.pending.to_dict() if self.pending else None
            return json.loads(json.dumps(data, default=str))

    def save(self) -> "RunState":
        with self._lock:
            self.updated_at = _now()
            if self.started_at is not None:
                end = self.finished_at if self.finished_at is not None else time.time()
                self.wall_seconds = round(end - self.started_at, 2)
            _atomic_write(self.path, self.to_dict())
        return self

    @classmethod
    def from_dict(cls, data: dict, runs_dir=None) -> "RunState":
        state = cls(data["run_id"], data.get("brief"), data.get("options"), runs_dir)
        for key in cls.FIELDS:
            if key in ("run_id", "brief", "options", "pending"):
                continue
            if key in data:
                setattr(state, key, data[key])
        if state.status not in STATUSES:
            state.status = "stopped"
        pending = data.get("pending")
        state.pending = PendingApproval.from_dict(pending) if isinstance(pending, dict) else None
        return state

    @classmethod
    def load(cls, run_id: str, runs_dir=None) -> "RunState":
        path = state_path(run_id, runs_dir)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as e:
            raise RunStateError(f"no saved state for run {run_id}") from e
        except (OSError, json.JSONDecodeError) as e:
            raise RunStateError(f"the saved state of run {run_id} could not be read: {e}") from e
        if not isinstance(data, dict) or data.get("run_id") != run_id:
            raise RunStateError(f"the saved state of run {run_id} is not valid")
        return cls.from_dict(data, runs_dir)

    @classmethod
    def new(cls, run_id: str, brief: dict | None = None, options: dict | None = None, runs_dir=None) -> "RunState":
        state = cls(run_id, brief, options, runs_dir)
        state.started_at = time.time()
        return state.save()

    # --- mutators (each one saves) -----------------------------------------
    def set_status(self, status: str, message: str | None = None, final_outcome: str | None = None,
                   stop_reasons=None) -> "RunState":
        if status not in STATUSES:
            raise RunStateError(f"status must be one of {STATUSES}")
        with self._lock:
            self.status = status
            if message is not None:
                self.message = message
            if final_outcome is not None:
                self.final_outcome = final_outcome
            if stop_reasons is not None:
                self.stop_reasons = list(stop_reasons)
            if status in ("stopped", "finished") and self.finished_at is None:
                self.finished_at = time.time()
            return self.save()

    def add_step(self, record: dict, tokens: int = 0, cost: float = 0) -> "RunState":
        with self._lock:
            self.steps.append(dict(record))
            self.tokens += int(tokens or 0)
            self.cost += float(cost or 0)
            if record.get("file") and record["file"] not in self.files:
                self.files.append(record["file"])
            return self.save()

    def add_gate(self, record: dict) -> "RunState":
        with self._lock:
            self.gates.append(dict(record))
            if record.get("file") and record["file"] not in self.files:
                self.files.append(record["file"])
            return self.save()

    def update_last_gate(self, **changes) -> "RunState":
        with self._lock:
            if self.gates:
                self.gates[-1].update(changes)
            return self.save()

    def set_cursor(self, **values) -> "RunState":
        with self._lock:
            self.cursor.update(json.loads(json.dumps(values, default=str)))
            return self.save()

    def add_tokens(self, tokens: int = 0, cost: float = 0) -> "RunState":
        with self._lock:
            self.tokens += int(tokens or 0)
            self.cost += float(cost or 0)
            return self.save()

    # --- approval store protocol (used by approval.PendingApprover) -------
    def get_pending(self):
        with self._lock:
            return self.pending

    def set_pending(self, item: PendingApproval) -> None:
        with self._lock:
            self.pending = item
            self.status = "awaiting_approval"
            self.save()

    def clear_pending(self) -> None:
        with self._lock:
            self.pending = None
            if self.status == "awaiting_approval":
                self.status = "running"
            self.save()

    def record_decision(self, item: PendingApproval) -> None:
        with self._lock:
            self.pending = item
            self.decisions.append(item.to_dict())
            if self.status == "awaiting_approval":
                self.status = "running"
            self.save()


def load(run_id: str, runs_dir=None) -> RunState:
    """Load ``runs/<run_id>/state.json``. Raises RunStateError with a plain message."""
    return RunState.load(run_id, runs_dir)


def list_runs(runs_dir=None) -> list:
    """Saved runs, newest first: [{"run_id", "status", "idea", "updated_at"}]."""
    root = runs_root(runs_dir)
    rows = []
    if not root.is_dir():
        return rows
    for path in root.glob(f"*/{STATE_FILE}"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            rows.append({"run_id": data["run_id"], "status": data.get("status"),
                         "idea": (data.get("brief") or {}).get("idea", ""),
                         "updated_at": data.get("updated_at") or ""})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    rows.sort(key=lambda r: (r["updated_at"], r["run_id"]), reverse=True)
    return rows
