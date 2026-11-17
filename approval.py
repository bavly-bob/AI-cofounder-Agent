"""Founder control, enforced in code.

Nothing is published, sent, spent or signed unless an ``Approver`` returns an
explicit approval for that one action. There is no flag, setting or environment
variable that approves on the founder's behalf: the only approvers in app code
are ``CLIApprover`` (asks the founder, default No) and ``DenyAllApprover``
(always No, used whenever nobody can be asked).

A UI approver (for example Streamlit) plugs in by subclassing ``Approver`` and
implementing ``request`` and, if it wants gate overrides, ``review_gate``.

``PendingApprover`` is the browser path: it never blocks and never approves.
When asked, it saves a ``PendingApproval`` and answers ``awaiting_approval``
(which is NOT an approval, so no executor runs). The founder's answer arrives
later through ``PendingApprover.decide``; the next time the same question is
asked, the recorded founder answer is returned.
"""
from __future__ import annotations

import contextvars
import functools
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

KINDS = ("publish", "send", "spend", "sign")
GATE_KIND = "gate"
GATE_OUTCOMES = ("go", "repeat", "pivot", "stop")
FOUNDER = "founder"
AWAITING = "awaiting_approval"


class ApprovalRequired(RuntimeError):
    """A founder-gated function was called outside ``guarded_action``."""


@dataclass(frozen=True)
class ApprovalResult:
    approved: bool
    approver: str | None
    note: str = ""
    status: str = "answered"     # "answered" | AWAITING (no decision yet: never an approval)

    @property
    def awaiting(self) -> bool:
        return self.status == AWAITING


@dataclass(frozen=True)
class GateReview:
    """The founder's answer to a gate recommendation.

    ``founder_outcome`` is one of GATE_OUTCOMES, or None when the founder gave
    no decision (the system recommendation then stands, without founder approval).
    """
    founder_outcome: str | None
    approver: str | None
    note: str = ""
    status: str = "answered"     # "answered" | AWAITING

    @property
    def awaiting(self) -> bool:
        return self.status == AWAITING


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


# --- browser path: pending approvals -----------------------------------------

class ApprovalError(ValueError):
    """A founder decision could not be recorded (unknown id, already decided, missing note...)."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class PendingApproval:
    """One question waiting for the founder: a publish/send/spend/sign action, or a gate."""
    id: str
    kind: str                         # one of KINDS, or GATE_KIND
    summary: str
    payload_preview: str
    recommendation: str | None        # gate: the system outcome; action: None (default if unanswered: No)
    created_at: str
    options: list = field(default_factory=list)   # gate: GATE_OUTCOMES
    details: dict = field(default_factory=dict)   # gate card: gate, target, actual, reasons, change_conditions...
    status: str = "pending"           # "pending" | "approved" | "overridden" | "rejected"
    decided_at: str | None = None
    founder_outcome: str | None = None
    note: str = ""
    approved_by: str | None = None    # FOUNDER only for an approval or an override

    @property
    def is_gate(self) -> bool:
        return self.kind == GATE_KIND

    @property
    def decided(self) -> bool:
        return self.status != "pending"

    def to_dict(self) -> dict:
        return json.loads(json.dumps(asdict(self), default=str))

    @classmethod
    def from_dict(cls, data: dict) -> "PendingApproval":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in dict(data).items() if k in known})

    def answer(self):
        """The recorded founder decision as the engine expects it. Pending => awaiting (never approved)."""
        if not self.decided:
            if self.is_gate:
                return GateReview(None, None, "awaiting the founder's decision", status=AWAITING)
            return ApprovalResult(False, None, "awaiting the founder's decision", status=AWAITING)
        if self.is_gate:
            # A gate Reject halts the run: the founder chose STOP.
            return GateReview(self.founder_outcome, FOUNDER, self.note or self.status)
        if self.status == "approved":
            return ApprovalResult(True, FOUNDER, self.note or "approved by the founder in the app")
        return ApprovalResult(False, FOUNDER, self.note or "rejected by the founder in the app")


class MemoryApprovalStore:
    """Keeps the pending item in memory. ``run_state.RunState`` is the persisted store."""

    def __init__(self):
        self.pending: PendingApproval | None = None
        self.decisions: list = []

    def get_pending(self):
        return self.pending

    def set_pending(self, item) -> None:
        self.pending = item

    def clear_pending(self) -> None:
        self.pending = None

    def record_decision(self, item) -> None:
        self.pending = item
        self.decisions.append(item.to_dict())


class PendingApprover(Approver):
    """Never blocks, never approves on its own.

    ``request`` / ``review_gate``: if the founder already decided this exact
    question, return that decision (once); otherwise save a PendingApproval in
    ``store`` and return an ``awaiting_approval`` answer, which is not an approval.

    ``decide``: the only way a decision gets in. It is called from the UI when the
    founder clicks a button.
    """

    def __init__(self, store=None, logger=None):
        self.store = store if store is not None else MemoryApprovalStore()
        self.logger = logger
        self._context: dict = {}

    def set_context(self, details: dict) -> None:
        """Extra facts for the NEXT question (e.g. a gate's target, actual, reasons)."""
        self._context = dict(details or {})

    def _next_id(self, kind: str) -> str:
        count = len(getattr(self.store, "decisions", []) or []) + 1
        return f"apr-{count:03d}-{kind}"

    def _ask(self, kind, summary, payload, recommendation=None, options=None):
        current = self.store.get_pending()
        if current is not None and current.kind == kind and current.summary == summary:
            if current.decided:
                self.store.clear_pending()   # each decision answers exactly one question
            return current.answer()
        if current is not None and not current.decided:
            # A different question while one is still open: never stack them; the open one stays.
            note = f"another approval ({current.id}) is still waiting for the founder"
            if kind == GATE_KIND:
                return GateReview(None, None, note, status=AWAITING)
            return ApprovalResult(False, None, note, status=AWAITING)
        item = PendingApproval(
            id=self._next_id(kind), kind=kind, summary=str(summary),
            payload_preview=_preview(payload), recommendation=recommendation, created_at=_now(),
            options=list(options or []), details=self._context,
        )
        self._context = {}
        self.store.set_pending(item)
        return item.answer()

    def request(self, kind, summary, payload) -> ApprovalResult:
        if kind not in KINDS:
            return ApprovalResult(False, None, f"unknown action kind {kind!r}: denied")
        return self._ask(kind, summary, payload)

    def review_gate(self, summary, system_outcome, options) -> GateReview:
        return self._ask(GATE_KIND, summary, {"system_outcome": system_outcome},
                         recommendation=system_outcome, options=options)

    def decide(self, approval_id: str, approved: bool, founder_outcome: str | None = None,
               note: str = "") -> PendingApproval:
        """Record the founder's decision on the pending item ``approval_id``.

        - approved=True, no outcome (or the recommended one): the founder confirms.
        - approved=True with a different gate outcome: an OVERRIDE; ``note`` is required.
        - approved=False: rejected. For a gate this halts the run (STOP).
        Raises ApprovalError for an unknown id, a second decision or a missing note.
        """
        item = self.store.get_pending()
        if item is None or item.id != approval_id:
            raise ApprovalError(f"there is no pending approval with id {approval_id!r}")
        if item.decided:
            raise ApprovalError(f"approval {approval_id} was already decided ({item.status})")
        if approved is not True and approved is not False:
            raise ApprovalError("approved must be True or False")
        note = (note or "").strip()
        if founder_outcome is not None:
            founder_outcome = str(founder_outcome).strip().lower()
            if not item.is_gate:
                raise ApprovalError("an outcome can only be chosen for a gate")
            if founder_outcome not in GATE_OUTCOMES:
                raise ApprovalError(f"outcome must be one of {GATE_OUTCOMES}")
            if not approved:
                raise ApprovalError("Reject takes no outcome; use Override to choose a different outcome")
        if approved:
            outcome = founder_outcome or item.recommendation
            if item.is_gate and outcome != item.recommendation:
                if not note:
                    raise ApprovalError("an override needs a note explaining why")
                item.status = "overridden"
            else:
                item.status = "approved"
            item.founder_outcome = outcome if item.is_gate else None
            item.approved_by = FOUNDER
        else:
            item.status = "rejected"
            item.founder_outcome = "stop" if item.is_gate else None
            item.approved_by = None
        item.note, item.decided_at = note, _now()
        if self.logger is not None:
            self.logger.log(
                "approval", "founder_decision", approved_by=item.approved_by, approval_id=item.id,
                kind=item.kind, decision=item.status, outcome=item.founder_outcome or item.status,
                system_outcome=item.recommendation, founder_outcome=item.founder_outcome,
                override=item.status == "overridden", note=note, summary=item.summary,
            )
        self.store.record_decision(item)
        return item


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

        # functools.wraps sets __wrapped__ (the undecorated function). Remove it so the
        # approval check cannot be bypassed through the wrapper; name and doc stay.
        del wrapper.__wrapped__
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
    # "approved" | "rejected" (explicit founder No) | "denied_by_default" | AWAITING (paused, not run)
    decision: str = "denied_by_default"


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
    awaiting = isinstance(answer, ApprovalResult) and answer.awaiting
    approved = (
        isinstance(answer, ApprovalResult) and not awaiting
        and answer.approved is True and answer.approver == FOUNDER
    )
    note = getattr(answer, "note", "") or ""
    if awaiting:
        # Nobody has decided yet: the executor does not run, and this is not a rejection either.
        _log("approval_pending", outcome="pending", decision=AWAITING, executed=False, note=note)
        return ActionResult(kind, False, False, None, note, decision=AWAITING)
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
