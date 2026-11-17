"""The step contract every agent implements, the step registry, and two STUBBED dummy steps.

Later milestones add a step by writing a class with ``phase``, ``step``,
``name``, ``agent`` and ``run(ctx)``, and decorating it with ``@register_step``.
A step may also define ``default_gates()`` to say which gates follow it. The
orchestrator does not need to change.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from decision_log import is_http_url

STATUSES = ("WORKING", "MOCKED", "STUBBED")
OUTPUT_NAME_RE = re.compile(r"^phase\d+_(step\d+|gate)_[a-z0-9_]+\.(md|json)$")
# Extra text files a step may write next to its Markdown file (e.g. the Phase 2 landing page).
EXTRA_OUTPUT_NAME_RE = re.compile(r"^phase\d+_step\d+_[a-z0-9_]+\.(html|csv|txt)$")


@dataclass
class Claim:
    text: str
    source_url: str | None = None      # None => rendered as "ASSUMPTION"
    source_label: str | None = None    # a non-URL source, e.g. "founder interview notes" (never counts as a URL source)

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
    llm_client: object = None    # has .generate_json(prompt, schema, system, name); None => local Ollama
    research: object = None      # tools.research.Research for this run (budget, cache, source registry)
    echo: object = None          # progress printer for the CLI

    def progress(self, message: str) -> None:
        if callable(self.echo):
            self.echo(message)

    def llm_json(self, prompt: str, schema: dict, agent: str, system: str | None = None,
                 name: str | None = None) -> dict:
        """Call the model for schema-checked JSON and log the call (also when it fails)."""
        client = self.llm_client
        if client is None:
            from llm import OllamaClient
            client = self.llm_client = OllamaClient()
        started = time.monotonic()
        try:
            reply = client.generate_json(prompt, schema, system=system, name=name)
            if not isinstance(reply, dict) or reply.get("error") or not isinstance(reply.get("data"), dict):
                from llm import LLMError
                detail = reply.get("error") if isinstance(reply, dict) else None
                raise LLMError(detail or "the model returned no usable JSON")
        except Exception as e:
            self.logger.log(agent, "llm_call", model=getattr(client, "model", None) or _default_model(),
                            cost=0, tokens=None, call=name, error=f"{type(e).__name__}: {e}",
                            seconds=round(time.monotonic() - started, 2))
            raise
        self.logger.log(agent, "llm_call", model=reply.get("model"), cost=reply.get("cost", 0) or 0,
                        tokens=reply.get("tokens"), call=name, attempts=reply.get("attempts"),
                        seconds=round(time.monotonic() - started, 2))
        return reply

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


# --- saving and restoring results (resume) ----------------------------------

def result_to_dict(result: StepResult) -> dict:
    return {"phase": result.phase, "step": result.step, "name": result.name, "agent": result.agent,
            "content_md": result.content_md, "data": result.data, "model": result.model, "cost": result.cost,
            "tokens": result.tokens, "status": result.status,
            "claims": [{"text": c.text, "source_url": c.source_url, "source_label": getattr(c, "source_label", None)}
                       for c in result.claims]}


def result_from_dict(data: dict) -> StepResult:
    claims = [Claim(c.get("text", ""), c.get("source_url"), c.get("source_label")) for c in data.get("claims", [])]
    return StepResult(int(data["phase"]), int(data["step"]), data["name"], data["agent"], data.get("content_md", ""),
                      claims, dict(data.get("data") or {}), data.get("model"), data.get("cost", 0),
                      data.get("tokens"), data.get("status", "WORKING"))


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
    label = getattr(claim, "source_label", None)
    if label:
        return f"{claim.text} (source: {label}; not a web source)"
    return f"{claim.text} (ASSUMPTION)"


def render_claims(claims) -> str:
    return "\n".join(f"- {render_claim(c)}" for c in claims)


def render_step_markdown(result: StepResult, run_id: str) -> str:
    parts = [header_line(run_id, result.model, result.status), "", result.content_md.strip(), ""]
    if result.claims:
        parts += ["## Claims", "", render_claims(result.claims), ""]
    return "\n".join(parts)


def load_prompt(role: str) -> str:
    """The prompt file of an agent: prompts/<role>.md."""
    return (Path(__file__).resolve().parent / "prompts" / f"{role}.md").read_text(encoding="utf-8")


def _default_model() -> str:
    import os
    return os.getenv("OLLAMA_MODEL", "qwen3:8b")


# --- dummy steps (STUBBED placeholders) ------------------------------------

class DummyDiscoverStep:
    """STUBBED discover step: does no research and says so.

    Not registered any more: the real Phase 1 agents (agents/) replaced it in M2.
    It stays available for engine tests that need a step with no model and no network."""
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


class DummyValidateStep:
    """Not registered any more: the real Phase 2 agents (agents/) replaced it in M3. It stays
    available for the M1 engine tests, which pin the registry to the two dummy steps.

    STUBBED validate step. Its metric is MOCKED: it comes from the CLI flag
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
