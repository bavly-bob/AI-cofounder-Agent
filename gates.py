"""Gates: go / repeat / pivot / stop.

A gate states its target BEFORE the step runs, then judges the step's result.
Rules that hold for every gate:

- Missing or unsourced evidence never passes: it is a STOP.
- The system only recommends. The founder may confirm or override, and an
  override is logged with both outcomes. The system never upgrades a STOP.
- The written decision says plainly when a result failed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from approval import FOUNDER, GATE_OUTCOMES, guarded_action
from steps import header_line


class Outcome(str, Enum):
    GO = "go"
    REPEAT = "repeat"
    PIVOT = "pivot"
    STOP = "stop"


assert tuple(o.value for o in Outcome) == GATE_OUTCOMES


@dataclass
class GateDecision:
    outcome: Outcome
    reasons: list
    evidence: dict
    recommended_by: str = "system"
    gate: str = "gate"
    target: str = ""
    actual: str = ""
    change_conditions: list = field(default_factory=list)  # what evidence would change the decision
    basis: str = "WORKING"      # "STUBBED" / "MOCKED" when the judged step or evidence is not real
    reviewable: bool = True     # False: go/repeat/pivot/stop overrides do not apply (founder-approval gate)


@dataclass
class FinalDecision:
    """A gate decision after the founder had the chance to confirm or override it."""
    system_outcome: Outcome
    founder_outcome: Outcome | None
    outcome: Outcome                     # the one the orchestrator acts on
    approved_by: str | None
    note: str = ""


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _below_target(attempts_remaining: int):
    """Between half the target and the target: repeat while attempts remain, otherwise pivot."""
    if attempts_remaining > 0:
        return Outcome.REPEAT, f"Attempts remaining: {attempts_remaining}, so the step can be repeated."
    return Outcome.PIVOT, "No attempts remain, so repeating is not an option: pivot."


class Gate:
    name = "gate"

    def describe_target(self) -> dict:
        """What the gate will require. Logged before the step runs."""
        raise NotImplementedError

    def evaluate(self, result, ctx, attempts_remaining: int = 0) -> GateDecision:
        raise NotImplementedError


class EvidenceThresholdGate(Gate):
    """Compares one measured metric with a target fixed in advance.

    The step must report ``data["metrics"][metric] = {"value": <number>, "source": <where it came from>}``.
    """
    name = "evidence_threshold"

    def __init__(self, metric: str, target: float, comparator: str = ">=", window_desc: str = ""):
        if comparator not in (">=", "<="):
            raise ValueError("comparator must be '>=' or '<='")
        if not _is_number(target) or target <= 0:
            raise ValueError("target must be a positive number")
        self.metric, self.target, self.comparator, self.window_desc = metric, target, comparator, window_desc

    def _target_text(self) -> str:
        window = f" ({self.window_desc})" if self.window_desc else ""
        return f"{self.metric} {self.comparator} {self.target:g}{window}"

    def describe_target(self) -> dict:
        return {"gate": self.name, "metric": self.metric, "target": self.target,
                "comparator": self.comparator, "window": self.window_desc,
                "target_text": self._target_text()}

    def evaluate(self, result, ctx, attempts_remaining: int = 0) -> GateDecision:
        evidence = {"metric": self.metric, "target": self.target, "comparator": self.comparator,
                    "window": self.window_desc, "actual": None, "source": None}
        change = [f"A sourced measurement showing {self._target_text()}."]

        def decide(outcome, reasons, actual_text):
            return GateDecision(outcome, reasons, evidence, "system", self.name,
                                self._target_text(), actual_text, change)

        metrics = (getattr(result, "data", None) or {}).get("metrics")
        record = metrics.get(self.metric) if isinstance(metrics, dict) else None
        if not isinstance(record, dict) or not _is_number(record.get("value")):
            return decide(Outcome.STOP, [
                f"Missing evidence: the step reported no numeric value for '{self.metric}'.",
                "Missing evidence never passes a gate.",
            ], "no evidence")
        actual = record["value"]
        evidence["actual"] = actual
        source = record.get("source")
        if not isinstance(source, str) or not source.strip():
            return decide(Outcome.STOP, [
                f"Unsourced evidence: '{self.metric}' = {actual:g} has no stated source.",
                "Unsourced evidence never passes a gate.",
            ], f"{actual:g} (unsourced)")
        evidence["source"] = source
        actual_text = f"{actual:g} (source: {source})"
        mocked = bool(record.get("mocked"))
        evidence["mocked"] = mocked
        plain_decide = decide

        def decide(outcome, reasons, text):  # a MOCKED metric is labelled on every outcome
            decision = plain_decide(outcome, reasons, text)
            if mocked:
                decision.basis = "MOCKED"
                decision.reasons.append(
                    "The metric is MOCKED (typed in, not measured): this is not a real validation.")
            return decision

        if self.comparator == ">=":
            met, halfway = actual >= self.target, actual >= 0.5 * self.target
            share = f"{100 * actual / self.target:.0f}% of the target"
        else:
            met, halfway = actual <= self.target, actual <= 2 * self.target
            share = f"{actual / self.target:.1f}x the target (lower is better)"

        if met:
            return decide(Outcome.GO, [f"Target met: {actual:g} against {self._target_text()}."], actual_text)
        if halfway:
            outcome, why = _below_target(attempts_remaining)
            return decide(outcome, [
                f"Target NOT met: {actual:g} against {self._target_text()} ({share}).",
                "The result is at least halfway to the target, but it is still a miss.",
                why,
            ], actual_text)
        return decide(Outcome.STOP, [
            f"Target NOT met: {actual:g} against {self._target_text()} ({share}).",
            "The result is less than halfway to the target. The evidence is weak.",
        ], actual_text)


class SourcedClaimsGate(Gate):
    """Requires a minimum share of the step's claims to carry a source URL."""
    name = "sourced_claims"

    def __init__(self, min_sourced_ratio: float = 1.0):
        if not _is_number(min_sourced_ratio) or not 0 < min_sourced_ratio <= 1:
            raise ValueError("min_sourced_ratio must be in (0, 1]")
        self.min_sourced_ratio = min_sourced_ratio

    def _target_text(self) -> str:
        return f"at least {self.min_sourced_ratio:.0%} of claims cite a source URL"

    def describe_target(self) -> dict:
        return {"gate": self.name, "metric": "sourced_ratio", "target": self.min_sourced_ratio,
                "comparator": ">=", "target_text": self._target_text()}

    def evaluate(self, result, ctx, attempts_remaining: int = 0) -> GateDecision:
        claims = list(getattr(result, "claims", None) or [])
        sourced = sum(1 for c in claims if getattr(c, "sourced", False))
        evidence = {"metric": "sourced_ratio", "target": self.min_sourced_ratio,
                    "claims": len(claims), "sourced": sourced, "actual": None}
        change = [f"Claims backed by source URLs, so that {self._target_text()}."]

        def decide(outcome, reasons, actual_text):
            return GateDecision(outcome, reasons, evidence, "system", self.name,
                                self._target_text(), actual_text, change)

        if not claims:
            return decide(Outcome.STOP, ["Missing evidence: the step made no claims at all.",
                                         "Missing evidence never passes a gate."], "no claims")
        ratio = sourced / len(claims)
        evidence["actual"] = ratio
        actual_text = f"{sourced} of {len(claims)} claims sourced ({ratio:.0%}); the rest are labelled ASSUMPTION"
        if ratio >= self.min_sourced_ratio:
            return decide(Outcome.GO, [f"Target met: {sourced} of {len(claims)} claims cite a source."], actual_text)
        if ratio >= 0.5 * self.min_sourced_ratio:
            outcome, why = _below_target(attempts_remaining)
            return decide(outcome, [f"Target NOT met: only {sourced} of {len(claims)} claims cite a source.", why],
                          actual_text)
        return decide(Outcome.STOP, [
            f"Target NOT met: only {sourced} of {len(claims)} claims cite a source.",
            "Most of the claims are unsourced. The evidence is weak.",
        ], actual_text)


class StubPassThroughGate(Gate):
    """Lets a STUBBED step through so the engine can be exercised, and says so.

    It checks no evidence and never calls the result sourced or validated. For
    any step that is not STUBBED it is a STOP, so it cannot wave real work through.
    """
    name = "stub_passthrough"

    def describe_target(self) -> dict:
        return {"gate": self.name,
                "target_text": "none: the step is STUBBED, so there is no evidence to check"}

    def evaluate(self, result, ctx, attempts_remaining: int = 0) -> GateDecision:
        target = self.describe_target()["target_text"]
        status = getattr(result, "status", None)
        change = ["A real (not STUBBED) step with claims that cite retrieved sources."]
        if status != "STUBBED":
            return GateDecision(Outcome.STOP, [
                f"This gate only lets STUBBED steps through, and the step's status is {status}.",
                "No evidence was checked, and missing evidence never passes a gate.",
            ], {"step_status": status, "evidence_checked": False}, "system", self.name, target,
                "no evidence checked", change)
        return GateDecision(Outcome.GO, [
            "The step is STUBBED: it did no research and has no sourced claims.",
            "No evidence was checked. The run continues only so that the later steps can be exercised.",
            "This is a stub pass-through, not a finding about the idea.",
        ], {"step_status": status, "evidence_checked": False}, "system", self.name, target,
            "no evidence (STUBBED step)", change, basis="STUBBED")


class TestsPassGate(Gate):
    """Runs a callable that returns True/False, or (True/False, detail)."""
    name = "tests_pass"
    __test__ = False  # not a pytest test class

    def __init__(self, check, description: str = "all checks pass"):
        self.check, self.description = check, description

    def describe_target(self) -> dict:
        return {"gate": self.name, "target_text": self.description}

    def evaluate(self, result, ctx, attempts_remaining: int = 0) -> GateDecision:
        change = [f"A run in which {self.description}."]
        try:
            verdict = self.check()
        except Exception as e:
            return GateDecision(Outcome.STOP, [f"Missing evidence: the check could not be run ({e})."],
                                {"passed": None, "error": str(e)}, "system", self.name,
                                self.description, "check did not run", change)
        detail = ""
        if isinstance(verdict, tuple):
            verdict, detail = verdict[0], str(verdict[1]) if len(verdict) > 1 else ""
        evidence = {"passed": verdict is True, "detail": detail}
        if verdict is True:
            return GateDecision(Outcome.GO, ["The checks passed."], evidence, "system", self.name,
                                self.description, "passed", change)
        if attempts_remaining > 0:
            outcome, why = Outcome.REPEAT, f"Attempts remaining: {attempts_remaining}, so the step can be repeated."
        else:
            outcome, why = Outcome.STOP, "No attempts remain."
        reasons = ["The checks FAILED." + (f" Detail: {detail}" if detail else ""), why]
        return GateDecision(outcome, reasons, evidence, "system", self.name, self.description, "failed", change)


class FounderApprovalGate(Gate):
    """Asks the founder to approve one publish/send/spend/sign action, through approval.guarded_action.

    Approved: the executor runs and the gate is GO. Not approved: the executor
    never runs and the gate is STOP. The decision belongs to the founder.
    """
    name = "founder_approval"

    def __init__(self, kind: str, summary: str, executor, payload_fn=None):
        self.kind, self.summary, self.executor, self.payload_fn = kind, summary, executor, payload_fn

    def describe_target(self) -> dict:
        return {"gate": self.name, "kind": self.kind,
                "target_text": f"explicit founder approval for this {self.kind} action: {self.summary}"}

    def evaluate(self, result, ctx, attempts_remaining: int = 0) -> GateDecision:
        if self.payload_fn is not None:
            payload = self.payload_fn(result, ctx)
        else:
            payload = {"phase": getattr(result, "phase", None), "step": getattr(result, "step", None),
                       "name": getattr(result, "name", None), "status": getattr(result, "status", None)}
        action = guarded_action(self.kind, self.summary, payload, self.executor, ctx)
        evidence = {"kind": self.kind, "approved": action.approved, "executed": action.executed,
                    "decision": action.decision, "result": action.result, "note": action.note}
        target = self.describe_target()["target_text"]
        change = [f"The founder approving this {self.kind} action in a new run."]

        def decide(outcome, reasons, by, actual):
            return GateDecision(outcome, reasons, evidence, by, self.name, target, actual, change,
                                reviewable=False)

        if action.approved and action.executed:
            return decide(Outcome.GO, [f"The founder approved the {self.kind} action, and it was carried out."],
                          FOUNDER, "approved by the founder")
        if action.approved:
            return decide(Outcome.STOP,
                          [f"The founder approved the {self.kind} action, but it FAILED: {action.error}"],
                          "system", "approved, then failed")
        if action.decision == "rejected":  # an explicit No from the founder
            return decide(Outcome.STOP, [
                f"The founder rejected the {self.kind} action ({action.note or 'answered No'}).",
                "Nothing was published, sent, spent or signed.",
            ], FOUNDER, "rejected by the founder")
        return decide(Outcome.STOP, [
            f"The {self.kind} action was NOT approved: no founder decision was recorded, so it was "
            f"denied by default ({action.note or 'no answer'}).",
            "Nothing was published, sent, spent or signed.",
        ], "system", "no founder decision: denied by default")


# --- founder confirm / override ---------------------------------------------

def founder_review(decision: GateDecision, ctx, phase: int, step: int) -> FinalDecision:
    """Offer the gate recommendation to the founder. Only the founder can change an outcome."""
    system = decision.outcome
    if not decision.reviewable:
        # A founder-approval gate: its outcome follows the approval answer and nothing else.
        # A denied action cannot be turned into a GO at a gate prompt.
        answered = decision.recommended_by == FOUNDER
        approved = answered and decision.evidence.get("approved") is True
        return FinalDecision(system, system if answered else None, system, FOUNDER if approved else None,
                             str(decision.evidence.get("decision") or ""))
    summary = (f"Gate '{decision.gate}' after step {phase}.{step}: the system recommends "
               f"{system.value.upper()}.\n" + "\n".join(f"  - {r}" for r in decision.reasons))
    try:
        review = ctx.approver.review_gate(summary, system.value, list(GATE_OUTCOMES))
        chosen = getattr(review, "founder_outcome", None)
        by_founder = getattr(review, "approver", None) == FOUNDER and chosen in GATE_OUTCOMES
        note = getattr(review, "note", "") or ""
    except Exception as e:  # a broken approver never changes an outcome
        by_founder, chosen, note = False, None, f"approver failed: {e}"
    common = dict(gate=decision.gate, phase=phase, step=step, system_outcome=system.value)
    if not by_founder:
        ctx.logger.log("gate", "founder_review_none", outcome=system.value, founder_outcome=None,
                       note=note or "no founder decision: system recommendation stands", **common)
        return FinalDecision(system, None, system, None, note)
    founder = Outcome(chosen)
    action = "founder_confirm" if founder == system else "founder_override"
    ctx.logger.log("gate", action, approved_by=FOUNDER, outcome=founder.value,
                   founder_outcome=founder.value, note=note, **common)
    return FinalDecision(system, founder, founder, FOUNDER, note)


# --- Markdown ----------------------------------------------------------------

_HEADLINE = {
    Outcome.GO: "GO. The target was met.",
    Outcome.REPEAT: "REPEAT. The target was NOT met.",
    Outcome.PIVOT: "PIVOT. The target was NOT met, and repeating is not an option.",
    Outcome.STOP: "STOP. This did not pass.",
}

_OPTIONS = (
    "- `accept`: confirm the recommendation above.\n"
    "- `go`: continue to the next step.\n"
    "- `repeat`: run this step again (while attempts remain).\n"
    "- `pivot`: go back to an earlier step and change direction.\n"
    "- `stop`: halt the run.\n\n"
    "Choosing anything other than the recommendation is an override. It is logged with both "
    "the system outcome and the founder outcome. The system never upgrades a STOP by itself."
)


BASIS_LABEL = {
    "STUBBED": "STUBBED step \u2014 placeholder, not real evidence",
    "MOCKED": "MOCKED \u2014 not a real validation",
}


def outcome_label(decision: GateDecision) -> str:
    """ "GO", or "GO (MOCKED ...)" / "GO (STUBBED ...)" when the decision rests on something not real."""
    word = decision.outcome.value.upper()
    return f"{word} ({BASIS_LABEL[decision.basis]})" if decision.basis in BASIS_LABEL else word


def _founder_approval_lines(decision: GateDecision) -> list:
    kind, state = decision.evidence.get("kind"), decision.evidence.get("decision")
    if state == "approved":
        return [f"- The founder approved this {kind} action (approved_by: founder)."]
    if state == "rejected":
        return [f"- The founder rejected this {kind} action at the prompt. Nothing was done (approved_by: null)."]
    return ["- No founder decision was recorded. The action was denied by default (approved_by: null)."]


def render_decision_md(decision: GateDecision, run_id: str, phase: int, step: int,
                       final: FinalDecision | None = None, attempt: int = 1) -> str:
    headline = _HEADLINE[decision.outcome]
    if decision.basis in BASIS_LABEL:
        tail = headline.split(". ", 1)[1]
        if decision.outcome == Outcome.GO:  # never "the target was met" without saying on what
            tail = ("No evidence was checked." if decision.basis == "STUBBED"
                    else "The target was reached only by a MOCKED value.")
        headline = f"{outcome_label(decision)}. {tail}"
    lines = [
        header_line(run_id, None, decision.basis),
        "",
        f"# Gate decision: {decision.gate} (after step {phase}.{step}, attempt {attempt})",
        "",
        f"**Outcome: {headline}**",
        "",
        f"- Recommended by: {decision.recommended_by}",
        f"- Target (set before the step ran): {decision.target or 'n/a'}",
        f"- Actual: {decision.actual or 'n/a'}",
        "",
        "## Reasons",
        "",
        *[f"- {r}" for r in decision.reasons],
        "",
    ]
    if decision.outcome != Outcome.GO and decision.change_conditions:
        lines += ["## What evidence would change this decision", "",
                  *[f"- {c}" for c in decision.change_conditions], ""]
    if not decision.reviewable:
        lines += ["## Founder decision", "", *_founder_approval_lines(decision), ""]
        return "\n".join(lines)
    lines += ["## Founder options", "", _OPTIONS, ""]
    if final is not None:
        lines += ["## Founder decision", ""]
        if final.founder_outcome is None:
            lines.append(f"- No founder decision was recorded. The system recommendation "
                         f"({final.system_outcome.value.upper()}) stands.")
        elif final.founder_outcome == final.system_outcome:
            lines.append(f"- The founder decided {final.outcome.value.upper()} (approved_by: founder).")
        else:
            lines.append(f"- OVERRIDE by the founder: system outcome {final.system_outcome.value.upper()}, "
                         f"founder outcome {final.founder_outcome.value.upper()} (approved_by: founder).")
        lines.append("")
    return "\n".join(lines)
