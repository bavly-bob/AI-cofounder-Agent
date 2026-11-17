"""Founder control, enforced in code.

Nothing is published, sent, spent or signed unless an ``Approver`` returns an
explicit approval for that one action. There is no flag, setting or environment
variable that approves on the founder's behalf: the only approvers in app code
are ``CLIApprover`` (asks the founder, default No) and ``DenyAllApprover``
(always No, used whenever nobody can be asked).

A UI approver (for example Streamlit) plugs in by subclassing ``Approver`` and
implementing ``request`` and, if it wants gate overrides, ``review_gate``.
"""
from __future__ import annotations

import contextvars
import functools
import json
import sys
from dataclasses import dataclass

KINDS = ("publish", "send", "spend", "sign")
GATE_OUTCOMES = ("go", "repeat", "pivot", "stop")
FOUNDER = "founder"


class ApprovalRequired(RuntimeError):
    """A founder-gated function was called outside ``guarded_action``."""


@dataclass(frozen=True)
class ApprovalResult:
    approved: bool
    approver: str | None
    note: str = ""


@dataclass(frozen=True)
class GateReview:
    """The founder's answer to a gate recommendation.

    ``founder_outcome`` is one of GATE_OUTCOMES, or None when the founder gave
    no decision (the system recommendation then stands, without founder approval).
    """
    founder_outcome: str | None
    approver: str | None
    note: str = ""


class Approver:
    """Interface for every approver (CLI now, UI later)."""

    def request(self, kind: str, summary: str, payload) -> ApprovalResult:
        raise NotImplementedError

    def review_gate(self, summary: str, system_outcome: str, options) -> GateReview:
        return GateReview(None, None, "no founder decision recorded")


class DenyAllApprover(Approver):
    """Default when nothing is interactive: every request is refused."""

    def request(self, kind, summary, payload) -> ApprovalResult:
        return ApprovalResult(False, None, "no founder present: denied by default")


def _read_line(prompt: str) -> str | None:
    """Return the typed line, or None when stdin is closed or unreadable."""
    try:
        answer = input(prompt)
        if not _stdin_is_interactive():
            print()  # piped input leaves the cursor on the prompt line
        return answer
    except (EOFError, KeyboardInterrupt, OSError, ValueError):
        print()
        return None


def _stdin_is_interactive() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


class CLIApprover(Approver):
    """Asks the founder in the terminal. Anything except an explicit y/yes is No."""

    def request(self, kind, summary, payload) -> ApprovalResult:
        print("\n--- FOUNDER APPROVAL NEEDED ---")
        print(f"Action : {kind}")
        print(f"Summary: {summary}")
        print(f"Details: {_preview(payload)}")
        answer = _read_line("Approve? [y/N]: ")
        if answer is None:
            return ApprovalResult(False, None, "no answer (input closed): treated as No")
        if answer.strip().lower() in ("y", "yes"):
            return ApprovalResult(True, FOUNDER, "approved at the CLI prompt")
        return ApprovalResult(False, FOUNDER, "rejected at the CLI prompt")

    def review_gate(self, summary, system_outcome, options) -> GateReview:
        if not _stdin_is_interactive():
            return GateReview(None, None, "input is not interactive: system recommendation stands")
        print("\n--- GATE DECISION ---")
        print(summary)
        choices = "/".join(options)
        answer = _read_line(
            f"Type 'accept' to confirm {system_outcome.upper()}, or override with {choices} "
            "(Enter = no founder decision): "
        )
        answer = (answer or "").strip().lower()
        if answer in ("accept", "a"):
            return GateReview(system_outcome, FOUNDER, "founder confirmed the recommendation")
        if answer in options:
            return GateReview(answer, FOUNDER, "founder chose this outcome")
        return GateReview(None, None, "no founder decision recorded: system recommendation stands")


def _preview(payload, limit: int = 600) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=True, default=str)
    except (TypeError, ValueError):
        text = repr(payload)
    return text if len(text) <= limit else text[:limit] + "... (truncated)"


# --- enforcement -----------------------------------------------------------

_active_kind: contextvars.ContextVar = contextvars.ContextVar("active_approved_kind", default=None)


def requires_founder_approval(kind: str):
    """Decorator: the function runs only inside an approved ``guarded_action`` of the same kind."""
    if kind not in KINDS:
        raise ValueError(f"unknown action kind {kind!r}; expected one of {KINDS}")

    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if _active_kind.get() != kind:
                raise ApprovalRequired(
                    f"{fn.__name__} is a '{kind}' action and needs founder approval: "
                    "call it through approval.guarded_action"
                )
            return fn(*args, **kwargs)

        wrapper.requires_founder_approval = kind
        return wrapper

    return decorate


@dataclass
class ActionResult:
    kind: str
    approved: bool
    executed: bool
    result: object = None
    note: str = ""
    error: str | None = None
    decision: str = "denied_by_default"   # "approved" | "rejected" (explicit founder No) | "denied_by_default"


def guarded_action(kind: str, summary: str, payload, executor, ctx) -> ActionResult:
    """Ask the founder, and run ``executor(payload)`` ONLY if they approve. Everything is logged.

    ``ctx`` needs ``.approver`` and ``.logger`` (a RunContext has both). A missing
    approver means DenyAllApprover.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown action kind {kind!r}; expected one of {KINDS}")
    approver = getattr(ctx, "approver", None) or DenyAllApprover()
    logger = getattr(ctx, "logger", None)

    def _log(action, approved_by=None, **extra):
        if logger is not None:
            logger.log("approval", action, approved_by=approved_by, kind=kind, summary=summary, **extra)

    _log("approval_request", approver_type=type(approver).__name__)
    try:
        answer = approver.request(kind, summary, payload)
    except Exception as e:  # a broken approver must never turn into an approval
        answer = ApprovalResult(False, None, f"approver failed: {e}")
    approved = (
        isinstance(answer, ApprovalResult) and answer.approved is True and answer.approver == FOUNDER
    )
    note = getattr(answer, "note", "") or ""
    if not approved:
        # Only an explicit No from the founder is a rejection. Silence, a closed
        # input or a broken approver is "denied by default": nobody decided.
        explicit = (isinstance(answer, ApprovalResult) and answer.approved is False
                    and answer.approver == FOUNDER)
        decision = "rejected" if explicit else "denied_by_default"
        _log("approval_denied", outcome="denied", decision=decision, executed=False, note=note)
        return ActionResult(kind, False, False, None, note, decision=decision)

    _log("approval_granted", approved_by=FOUNDER, outcome="approved", decision="approved", note=note)
    token = _active_kind.set(kind)
    try:
        result = executor(payload)
    except Exception as e:
        _log("action_failed", approved_by=FOUNDER, outcome="error", executed=False, error=str(e))
        return ActionResult(kind, True, False, None, note, error=str(e), decision="approved")
    finally:
        _active_kind.reset(token)
    status = result.get("status") if isinstance(result, dict) else None
    _log(f"execute_{kind}", approved_by=FOUNDER, outcome="executed", executed=True, status=status)
    return ActionResult(kind, True, True, result, note, decision="approved")


# --- MOCKED executors (M1 has no real publishing, sending, spending or signing) ---

@requires_founder_approval("publish")
def mock_publish(payload) -> dict:
    """MOCKED: publishes nothing. Returns a record of what would have been published."""
    return {"status": "MOCKED", "kind": "publish", "detail": "MOCKED publish: nothing left this machine"}


@requires_founder_approval("send")
def mock_send(payload) -> dict:
    """MOCKED: sends nothing. Returns a record of what would have been sent."""
    return {"status": "MOCKED", "kind": "send", "detail": "MOCKED send: no message was sent"}
