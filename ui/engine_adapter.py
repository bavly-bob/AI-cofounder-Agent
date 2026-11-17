"""The only door from the UI into the engine.

The UI calls exactly three functions, the ones the orchestrator will provide:

    start_run(brief, options) -> run_id
    step_until_pause(run_id) -> RunState
    submit_decision(run_id, approval_id, approved, founder_outcome=None, note="") -> RunState

INTERIM implementation (until the orchestrator has its own pause/resume, M4):
the existing ``orchestrator.run_pipeline`` runs in a background thread with a
``PendingApprover`` that saves each question in ``runs/<run_id>/state.json`` and
waits there until the founder clicks a button. Nothing is approved without that
click. Limitation: after an app restart the pending item is still shown and a
decision can be recorded, but the interrupted run cannot continue (it is marked
stopped with a plain message). The orchestrator integration removes this limit.
"""
from __future__ import annotations

import inspect
import threading
import time
from pathlib import Path

from approval import AWAITING, PendingApprover
from brief import FounderBrief
from decision_log import DecisionLogger, new_run_id
from run_state import RunState, RunStateError, list_runs, load

ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = ROOT              # run files go to BASE_DIR / "runs" / <run_id> (tests point this at a tmp dir)
WAIT_SECONDS = 240           # how long step_until_pause waits for the next pause before returning
DECISION_WAIT_HOURS = 12     # an unanswered question is denied by default after this long (never approved)

RESTART_MESSAGE = ("This run was interrupted (the app restarted). Your decision is recorded, but an "
                   "interrupted run cannot continue yet: start a new run. "
                   "(Engine integration pending, M4.)")


class EngineError(RuntimeError):
    """Plain-language problem the UI can show as it is."""


def runs_dir() -> Path:
    return Path(BASE_DIR) / "runs"


_LIVE: dict = {}             # run_id -> _LiveRun (only runs started by this process)
_LIVE_LOCK = threading.Lock()


class _LiveRun:
    def __init__(self, state: RunState):
        self.state = state
        self.wake = threading.Event()
        self.done = threading.Event()
        self.thread: threading.Thread | None = None


class _WaitingApprover(PendingApprover):
    """A PendingApprover whose worker thread waits for the founder's click.

    It returns only (a) the founder's recorded decision, or (b) after
    DECISION_WAIT_HOURS, the ``awaiting_approval`` answer, which is NOT an approval.
    """

    def __init__(self, live: _LiveRun, logger):
        super().__init__(store=live.state, logger=logger)
        self.live = live

    def _ask(self, kind, summary, payload, recommendation=None, options=None):
        deadline = time.monotonic() + DECISION_WAIT_HOURS * 3600
        answer = super()._ask(kind, summary, payload, recommendation, options)
        while getattr(answer, "status", None) == AWAITING and time.monotonic() < deadline:
            self.live.wake.wait(timeout=1.0)
            self.live.wake.clear()
            answer = super()._ask(kind, summary, payload, recommendation, options)
        if kind == "gate" and getattr(answer, "status", None) != AWAITING:
            last = self.live.state.decisions[-1] if self.live.state.decisions else {}
            self.live.state.update_last_gate(founder_outcome=answer.founder_outcome,
                                             outcome=answer.founder_outcome,
                                             decision=last.get("status"), note=last.get("note", ""),
                                             approved_by=last.get("approved_by"))
        return answer


class _StepProxy:
    """Wraps a step to record its timing and result in the run state (the orchestrator is unchanged)."""

    def __init__(self, inner, live: _LiveRun):
        self._inner, self._live = inner, live

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def run(self, ctx):
        started = time.monotonic()
        result = self._inner.run(ctx)
        self._live.state.add_step({
            "id": f"{self._inner.phase}.{self._inner.step}", "phase": self._inner.phase,
            "step": self._inner.step, "name": getattr(result, "name", self._inner.name),
            "agent": getattr(result, "agent", self._inner.agent),
            "status": getattr(result, "status", None), "model": getattr(result, "model", None),
            "tokens": getattr(result, "tokens", None) or 0,
            "seconds": round(time.monotonic() - started, 2),
        }, tokens=getattr(result, "tokens", 0) or 0)
        return result


class _GateProxy:
    """Wraps a gate so the gate card can show target, actual, reasons and what would change it."""

    def __init__(self, inner, live: _LiveRun, approver: PendingApprover, phase: int, step: int):
        self._inner, self._live, self._approver = inner, live, approver
        self._phase, self._step = phase, step

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def evaluate(self, result, ctx, attempts_remaining: int = 0):
        target = {}
        try:
            target = self._inner.describe_target() or {}
        except Exception:
            pass
        base = {"gate": getattr(self._inner, "name", "gate"), "phase": self._phase, "step": self._step,
                "target": target.get("target_text", "")}
        self._approver.set_context(base)  # used if the gate itself asks (founder-approval gates)
        decision = self._inner.evaluate(result, ctx, attempts_remaining=attempts_remaining)
        card = {**base, "target": decision.target or base["target"], "actual": decision.actual,
                "reasons": list(decision.reasons), "change_conditions": list(decision.change_conditions),
                "basis": decision.basis, "system_outcome": decision.outcome.value,
                "reviewable": decision.reviewable}
        self._live.state.add_gate({"after": f"{self._phase}.{self._step}", **card,
                                   "founder_outcome": None, "outcome": decision.outcome.value})
        self._approver.set_context(card if decision.reviewable else {})
        return decision


def _engine():
    try:
        import orchestrator
    except Exception as e:  # e.g. a teammate's file is mid-edit
        raise EngineError(f"The engine could not be loaded ({type(e).__name__}). Try again in a moment.") from e
    return orchestrator


def _engine_kwargs(run_pipeline, options: dict) -> tuple:
    """Pass only the options this version of the engine understands; say which were not used."""
    params = inspect.signature(run_pipeline).parameters
    kwargs, unused = {}, []
    wanted = {
        "fast": options.get("fast"),
        "mock_signups": options.get("mock_signups"), "mock_visitors": options.get("mock_visitors"),
        "signups": options.get("signups"), "visitors": options.get("visitors"),
        "waitlist_rows": options.get("waitlist_rows"),
    }
    for key, value in wanted.items():
        if value is None or value is False:
            continue
        if key in params:
            kwargs[key] = value
        elif key == "mock_signups" and "metric" in params:
            kwargs["metric"] = float(value)   # the M1 engine reads its MOCKED metric from here
        else:
            unused.append(key)
    if "phases" in params and options.get("max_phase"):
        kwargs["phases"] = list(range(1, int(options["max_phase"]) + 1))
    return kwargs, unused


def start_run(brief: FounderBrief, options: dict | None = None, *, steps=None) -> str:
    """Create the run state and start the run in the background. Returns the run id.

    ``steps`` replaces the registered steps (tests only use it to stay offline).
    """
    options = dict(options or {})
    engine = _engine()
    run_id = new_run_id()
    state = RunState.new(run_id, brief.to_dict(), options, runs_dir())
    live = _LiveRun(state)
    logger = DecisionLogger(state.run_dir / "decision_log.json", run_id=run_id)
    approver = _WaitingApprover(live, logger)

    all_steps = list(steps) if steps is not None else engine.registered_steps()
    max_phase = options.get("max_phase")
    kwargs, unused = _engine_kwargs(engine.run_pipeline, options)
    if max_phase and "phases" not in kwargs:
        all_steps = [s for s in all_steps if int(s.phase) <= int(max_phase)]
    plan = engine.build_gate_plan(all_steps)
    proxies = [_StepProxy(s, live) for s in all_steps]
    gates = {key: [_GateProxy(g, live, approver, key[0], key[1]) for g in plan.get(key, [])]
             for key in plan}
    if unused:
        state.message = ("Not used by this engine version (arrives with the Phase 2 engine): "
                         + ", ".join(unused) + ".")
        state.save()

    def work():
        try:
            summary = engine.run_pipeline(brief, steps=proxies, gates=gates, approver=approver,
                                          base_dir=BASE_DIR, run_id=run_id, **kwargs)
            _finish(state, summary)
        except BaseException as e:  # never let a worker crash silently
            state.set_status("stopped", message=f"The run stopped because of an error: {type(e).__name__}: {e}")
        finally:
            live.done.set()

    live.thread = threading.Thread(target=work, name=f"run-{run_id}", daemon=True)
    with _LIVE_LOCK:
        _LIVE[run_id] = live
    live.thread.start()
    return run_id


def _finish(state: RunState, summary) -> None:
    timings = {}   # the live records (timing, model) per step id, in the order they ran
    for rec in state.steps:
        timings.setdefault(rec.get("id"), []).append(rec)
    steps = []
    for item in summary.steps:
        mine = timings.get(item["id"], [])
        extra = mine.pop(0) if mine else {}
        steps.append({**extra, **item})
    with state._lock:
        state.steps = steps
        state.files = list(summary.files)
        state.tokens = int(summary.total_tokens or 0)
        state.cost = float(summary.total_cost or 0)
        state.output_dir = str(summary.output_dir) if summary.output_dir else None
        state.log_path = str(summary.log_path) if summary.log_path else None
        state.pending = None if (state.pending is None or state.pending.decided) else state.pending
    status = "finished" if summary.status == "completed" else "stopped"
    state.set_status(status, final_outcome=summary.final_outcome, stop_reasons=summary.stop_reasons)


def _paused_or_done(live: _LiveRun) -> bool:
    with live.state._lock:   # mutators save while holding the lock, so what we see is on disk
        pending = live.state.pending
        return live.done.is_set() or (pending is not None and not pending.decided)


def step_until_pause(run_id: str, wait_seconds: float | None = None) -> RunState:
    """Wait until the run needs the founder, ends, or ``wait_seconds`` pass. Returns the saved state."""
    with _LIVE_LOCK:
        live = _LIVE.get(run_id)
    if live is None:
        state = load(run_id, runs_dir())
        if state.status == "running":
            state.set_status("stopped", message=RESTART_MESSAGE)
        return state
    deadline = time.monotonic() + (WAIT_SECONDS if wait_seconds is None else wait_seconds)
    while not _paused_or_done(live) and time.monotonic() < deadline:
        time.sleep(0.05)
    return live.state


def submit_decision(run_id: str, approval_id: str, approved: bool, founder_outcome: str | None = None,
                    note: str = "", wait_seconds: float | None = None) -> RunState:
    """Record the founder's decision (raises approval.ApprovalError on a bad one), then continue."""
    with _LIVE_LOCK:
        live = _LIVE.get(run_id)
    if live is None:
        state = load(run_id, runs_dir())
        logger = DecisionLogger(state.run_dir / "decision_log.json", run_id=run_id)
        PendingApprover(store=state, logger=logger).decide(approval_id, approved, founder_outcome, note)
        if state.status != "finished":
            state.set_status("stopped", message=RESTART_MESSAGE)
        return state
    approver = PendingApprover(store=live.state,
                               logger=DecisionLogger(live.state.run_dir / "decision_log.json", run_id=run_id))
    approver.decide(approval_id, approved, founder_outcome, note)
    live.wake.set()
    return step_until_pause(run_id, wait_seconds)


def get_state(run_id: str) -> RunState:
    with _LIVE_LOCK:
        live = _LIVE.get(run_id)
    return live.state if live is not None else load(run_id, runs_dir())


def is_live(run_id: str) -> bool:
    with _LIVE_LOCK:
        live = _LIVE.get(run_id)
    return live is not None and not live.done.is_set()


def saved_runs() -> list:
    return list_runs(runs_dir())


__all__ = ["start_run", "step_until_pause", "submit_decision", "get_state", "is_live", "saved_runs",
           "runs_dir", "EngineError", "RunStateError"]
