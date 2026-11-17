"""The step contract every agent implements, the step registry, and two STUBBED dummy steps.

Later milestones add a step by writing a class with ``phase``, ``step``,
``name``, ``agent`` and ``run(ctx)``, and decorating it with ``@register_step``.
A step may also define ``default_gates()`` to say which gates follow it. The
orchestrator does not need to change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from decision_log import is_http_url

STATUSES = ("WORKING", "MOCKED", "STUBBED")
OUTPUT_NAME_RE = re.compile(r"^phase\d+_(step\d+|gate)_[a-z0-9_]+\.(md|json)$")


@dataclass
class Claim:
    text: str
    source_url: str | None = None      # None => rendered as "ASSUMPTION"

    @property
    def sourced(self) -> bool:
        return is_http_url(self.source_url)


@dataclass
class StepResult:
    phase: int
    step: int
    name: str                                  # e.g. 1, 1, "opportunities"
    agent: str
    content_md: str
    claims: list = field(default_factory=list)
    data: dict = field(default_factory=dict)
    model: str | None = None
    cost: float = 0
    tokens: int | None = None
    status: str = "WORKING"                    # "WORKING" | "MOCKED" | "STUBBED"


@dataclass
class RunContext:
    """Everything a step can see or use during a run."""
    brief: object
    run_id: str
    logger: object
    approver: object
    output_dir: Path
    results: list = field(default_factory=list)
    options: dict = field(default_factory=dict)   # e.g. {"metric": 12.0} from the CLI

    def latest(self, phase: int, step: int):
        """The most recent result of a step, or None."""
        for result in reversed(self.results):
            if (result.phase, result.step) == (phase, step):
                return result
        return None

    def llm(self, prompt: str, agent: str, generate=None) -> dict:
        """Call the local model and log the call. Returns llm.generate's dict."""
        if generate is None:
            from llm import generate
        reply = generate(prompt)
        self.logger.log(
            agent, "llm_call", model=reply.get("model"), cost=reply.get("cost", 0) or 0,
            tokens=reply.get("tokens"), error=reply.get("error"),
        )
        return reply


@runtime_checkable
class Step(Protocol):
    phase: int
    step: int
    name: str
    agent: str

    def run(self, ctx: "RunContext") -> StepResult: ...


# --- registry --------------------------------------------------------------

_REGISTRY: dict = {}


def register_step(cls):
    """Class decorator: add a step to the pipeline, keyed by (phase, step)."""
    _REGISTRY[(int(cls.phase), int(cls.step))] = cls
    return cls


def registered_steps() -> list:
    """One fresh instance of every registered step, in phase/step order."""
    return [_REGISTRY[key]() for key in sorted(_REGISTRY)]


# --- rendering -------------------------------------------------------------

def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_") or "unnamed"


def output_filename(phase: int, kind: str, name: str, attempt: int = 1) -> str:
    """``phase<N>_step<M>_<name>.md`` or ``phase<N>_gate_<name>.md`` (kind is "step<M>" or "gate")."""
    suffix = f"_attempt{attempt}" if attempt > 1 else ""
    return f"phase{int(phase)}_{kind}_{slug(name)}{suffix}.md"


def header_line(run_id: str, model: str | None, status: str) -> str:
    return f"> run_id: {run_id} | model: {model or 'none (no AI used)'} | status: {status}"


def render_claim(claim: Claim) -> str:
    if claim.sourced:
        return f"{claim.text} [source]({claim.source_url})"
    return f"{claim.text} (ASSUMPTION)"


def render_claims(claims) -> str:
    return "\n".join(f"- {render_claim(c)}" for c in claims)


def render_step_markdown(result: StepResult, run_id: str) -> str:
    parts = [header_line(run_id, result.model, result.status), "", result.content_md.strip(), ""]
    if result.claims:
        parts += ["## Claims", "", render_claims(result.claims), ""]
    return "\n".join(parts)


# --- dummy steps (STUBBED: real agents arrive in a later milestone) --------

@register_step
class DummyDiscoverStep:
    """STUBBED discover step: does no research and says so."""
    phase, step, name, agent = 1, 1, "opportunities", "dummy_discover"

    def run(self, ctx: RunContext) -> StepResult:
        idea = getattr(ctx.brief, "idea", "")
        content = (
            "# Phase 1, step 1: opportunities (STUBBED)\n\n"
            "STUBBED: this is a placeholder step. No market research was carried out, "
            "so nothing below is evidence about the market.\n\n"
            f"Founder idea, as given: {idea}"
        )
        # No source was retrieved, so no URL is cited: every claim here is an ASSUMPTION.
        claims = [
            Claim("The idea addresses a problem that the target market actually has.", None),
            Claim("People in the target market would pay for a digital product that solves it.", None),
        ]
        return StepResult(self.phase, self.step, self.name, self.agent, content, claims,
                          {"stubbed": True}, None, 0, None, "STUBBED")

    def default_gates(self) -> list:
        from gates import StubPassThroughGate
        return [StubPassThroughGate()]


_NOTE_METRIC_RE = re.compile(r"mock[_ ]metric\s*[:=]\s*(-?\d+(?:\.\d+)?)", re.I)


@register_step
class DummyValidateStep:
    """STUBBED validate step. Its metric is MOCKED: it comes from the CLI flag
    (``--metric``) or from ``mock_metric=<n>`` in the brief's notes, never from
    a random number. With neither, it reports no evidence at all."""
    phase, step, name, agent = 2, 1, "validation", "dummy_validate"
    metric = "signups"
    target = 100
    window_desc = "MOCKED sign-ups in a 7-day test window"

    def run(self, ctx: RunContext) -> StepResult:
        value, source = None, None
        if ctx.options.get("metric") is not None:
            value, source = float(ctx.options["metric"]), "MOCKED: --metric CLI flag"
        else:
            match = _NOTE_METRIC_RE.search(getattr(ctx.brief, "notes", "") or "")
            if match:
                value, source = float(match.group(1)), "MOCKED: mock_metric in the brief's notes"
        shown = "none (no value was given)" if value is None else f"{value:g}"
        content = (
            "# Phase 2, step 1: validation (STUBBED)\n\n"
            "STUBBED: this is a placeholder step. No test was run with real people.\n\n"
            f"- Metric: `{self.metric}` ({self.window_desc})\n"
            f"- Value: **{shown}** (MOCKED)\n"
            f"- Where the value came from: {source or 'nowhere: no evidence available'}\n\n"
            "MOCKED: the value is an input typed by the operator, not a measurement."
        )
        data = {"metrics": {}}
        if value is not None:
            data["metrics"][self.metric] = {"value": value, "source": source, "mocked": True}
        return StepResult(self.phase, self.step, self.name, self.agent, content, [],
                          data, None, 0, None, "STUBBED")

    def default_gates(self) -> list:
        from approval import mock_publish
        from gates import EvidenceThresholdGate, FounderApprovalGate
        return [
            EvidenceThresholdGate(self.metric, self.target, ">=", self.window_desc),
            FounderApprovalGate(
                "publish",
                "Publish the offer page draft (MOCKED executor: nothing is actually published)",
                mock_publish,
            ),
        ]
