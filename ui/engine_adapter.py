"""The only door from the UI into the engine: direct calls to the orchestrator's browser path.

    start_run(brief, options) -> run_id
    step_until_pause(run_id) -> RunState
    submit_decision(run_id, approval_id, approved, founder_outcome=None, note="") -> RunState

The orchestrator saves ``runs/<run_id>/state.json`` after every step, gate, pause and decision,
so a refresh or an app restart shows the same pending item and continues after the decision.
Nothing is approved without the founder's click (``approval.PendingApprover``).
"""
from __future__ import annotations

from pathlib import Path

from brief import FounderBrief
from run_state import RunState, RunStateError, list_runs, load

ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = ROOT              # run files go to BASE_DIR / "runs" / <run_id> (tests point this at a tmp dir)


class EngineError(RuntimeError):
    """Plain-language problem the UI can show as it is."""


def runs_dir() -> Path:
    return Path(BASE_DIR) / "runs"


def _engine():
    try:
        import orchestrator
    except Exception as e:  # e.g. a file is mid-edit
        raise EngineError(f"The engine could not be loaded ({type(e).__name__}). Try again in a moment.") from e
    return orchestrator


def start_run(brief: FounderBrief, options: dict | None = None, **engine) -> str:
    """Create the run. ``engine`` (e.g. steps, llm_client) is injected by tests only."""
    return _engine().start_run(brief, options, base_dir=BASE_DIR, **engine)


def step_until_pause(run_id: str, wait_seconds: float | None = None, **engine) -> RunState:
    """Run until the founder is needed or the run ends. ``wait_seconds`` is kept for old callers."""
    return _engine().step_until_pause(run_id, base_dir=BASE_DIR, **engine)


def submit_decision(run_id: str, approval_id: str, approved: bool, founder_outcome: str | None = None,
                    note: str = "", wait_seconds: float | None = None, **engine) -> RunState:
    """Record the founder's decision (raises approval.ApprovalError on a bad one), then continue."""
    return _engine().submit_decision(run_id, approval_id, approved, founder_outcome, note,
                                     base_dir=BASE_DIR, **engine)


def get_state(run_id: str) -> RunState:
    return load(run_id, runs_dir())


def is_live(run_id: str) -> bool:
    """True while this process is stepping the run."""
    return _engine().is_busy(run_id)


def saved_runs() -> list:
    return list_runs(runs_dir())


__all__ = ["start_run", "step_until_pause", "submit_decision", "get_state", "is_live", "saved_runs",
           "runs_dir", "EngineError", "RunStateError"]
