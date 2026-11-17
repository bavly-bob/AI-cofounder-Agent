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

from approval import AWAITING, FOUNDER, GATE_OUTCOMES, ApprovalPending, guarded_action
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

    def __init__(self, kind: str, summary: str, executor, payload_fn=None, reject_outcome: "Outcome" = None,
                 on_approved=None, name: str | None = None):
        self.kind, self.summary, self.executor, self.payload_fn = kind, summary, executor, payload_fn
        # An explicit founder No gives ``reject_outcome`` (default STOP; REPEAT = redo the step with the
        # founder's note). Silence / no founder is always STOP: denied by default.
        self.reject_outcome = reject_outcome or Outcome.STOP
        self.on_approved = on_approved   # callable(action_result, result, ctx) after an approved execution
        if name:
            self.name = name

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
        if action.decision == AWAITING:   # browser path: the founder has not answered yet; pause the run
            raise ApprovalPending(f"{self.kind} action waiting for the founder: {self.summary}")
        evidence = {"kind": self.kind, "approved": action.approved, "executed": action.executed,
                    "decision": action.decision, "result": action.result, "note": action.note}
        target = self.describe_target()["target_text"]
        change = [f"The founder approving this {self.kind} action in a new run."]

        def decide(outcome, reasons, by, actual):
            return GateDecision(outcome, reasons, evidence, by, self.name, target, actual, change,
                                reviewable=False)

        if action.approved and action.executed:
            extra = []
            if callable(self.on_approved):
                extra = list(self.on_approved(action, result, ctx) or [])
            return decide(Outcome.GO, [f"The founder approved the {self.kind} action, and it was carried out."]
                          + extra, FOUNDER, "approved by the founder")
        if action.approved:
            return decide(Outcome.STOP,
                          [f"The founder approved the {self.kind} action, but it FAILED: {action.error}"],
                          "system", "approved, then failed")
        if action.decision == "rejected" and self.reject_outcome == Outcome.REPEAT:
            note = action.note or "answered No"
            ctx.options[f"founder_note_{getattr(result, 'phase', 0)}_{getattr(result, 'step', 0)}"] = note
            return decide(Outcome.REPEAT, [
                f"The founder rejected this ({note}). The step is redone with the founder's note.",
                "Nothing was published, sent, spent or signed.",
            ], FOUNDER, "rejected by the founder: redo the step")
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
        review, by_founder, chosen, note = None, False, None, f"approver failed: {e}"
    if getattr(review, "awaiting", False) is True:   # browser path: pause; the founder decides later
        raise ApprovalPending(f"gate '{decision.gate}' after step {phase}.{step} waiting for the founder")
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
    # Shown for every outcome: for a GO it says what evidence would turn it into REPEAT/PIVOT/STOP.
    lines += ["## What evidence would change this decision", "",
              *[f"- {c}" for c in (decision.change_conditions
                                   or ["New evidence that contradicts the evidence above."])], ""]
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


# --- Phase 1 evidence gate (M2) ------------------------------------------------

def load_phase1_thresholds() -> dict:
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parent / "config" / "thresholds.json"
    return json.loads(path.read_text(encoding="utf-8"))["phase1"]


class Phase1EvidenceGate(Gate):
    """After step 1.4. Uses only sourced (retrieved URL), non-MOCKED evidence.

    STOP: too few sourced pain signals or domains, main score too low, a hard
    founder-fit blocker, or an excluded product. PIVOT: an alternative scores at
    least ``pivot_margin`` higher and has its own sourced signals. REPEAT step 1.1
    (at most once): borderline signals or score. GO otherwise, with open risks.
    """
    name = "discover"

    def __init__(self, thresholds: dict | None = None):
        self.t = dict(thresholds or load_phase1_thresholds())

    def _target_text(self) -> str:
        t = self.t
        return (f">= {t['min_pain_signals']} distinct sourced pain signals from >= {t['min_signal_domains']} "
                f"domains; main idea score >= {t['min_main_score']}/5; no hard founder-fit blocker; not an "
                f"excluded product (borderline: <= {t['borderline_max_signals']} signals or score <= "
                f"{t['borderline_max_score']} -> REPEAT once; an alternative >= {t['pivot_margin']} higher "
                f"with its own sourced signals -> PIVOT)")

    def describe_target(self) -> dict:
        return {"gate": self.name, "thresholds": dict(self.t), "target_text": self._target_text()}

    def evaluate(self, result, ctx, attempts_remaining: int = 0) -> GateDecision:
        import guardrails
        from agents.common import current_idea, domains, sourced_signals

        t = self.t
        signals = sourced_signals(ctx)
        n, doms = len(signals), domains(signals)
        scoring = ctx.latest(1, 2)
        sdata = (scoring.data if scoring else {}) or {}
        ideas = sdata.get("ideas") or []
        main = next((x for x in ideas if x.get("id") == "main"), None)
        main_total = main["total"] if main else None
        blocker = (sdata.get("hard_blocker") or "").strip()
        verdict = guardrails.check_brief(current_idea(ctx))
        excluded = not verdict.allowed and verdict.category == "excluded_product"
        repeats_done = max(0, sum(1 for r in ctx.results if (r.phase, r.step) == (1, 1))
                           - int(ctx.options.get("phase1_runs_before_pivot", 0)) - 1)
        alts = [x for x in ideas if x.get("id") != "main"]
        top_alt = max(alts, key=lambda x: x["total"], default=None)
        margin = (top_alt["total"] - main_total) if (top_alt and main_total is not None) else None
        alt_name = (top_alt.get("short") or top_alt["name"]) if top_alt else None

        def mark(ok):
            return "PASS" if ok else "FAIL"

        score_text = "none" if main_total is None else f"{main_total:.2f}"
        fetched = sum(1 for s in signals if s.get("source_kind") == "fetched page")
        snippets = sum(1 for s in signals if s.get("source_kind") == "search snippet")
        prev_n = ctx.options.get("phase1_prev_signal_count")
        no_new = repeats_done > 0 and prev_n is not None and n <= prev_n
        ctx.options["phase1_prev_signal_count"] = n
        rows = [
            f"Sourced pain signals: {n} (threshold >= {t['min_pain_signals']}) - {mark(n >= t['min_pain_signals'])}",
            f"Signal source text checked: {fetched} fetched page(s), {snippets} search snippet(s) "
            "(a signal counts only if its wording matches the retrieved text)",
            f"Distinct source domains: {len(doms)} (threshold >= {t['min_signal_domains']}) - "
            f"{mark(len(doms) >= t['min_signal_domains'])}" + (f" [{', '.join(doms)}]" if doms else ""),
            f"Main idea score: {score_text}/5 (threshold >= {t['min_main_score']}) - "
            f"{mark(main_total is not None and main_total >= t['min_main_score'])}",
            f"Hard founder-fit blocker: {blocker or 'none'} - {mark(not blocker)}",
            f"Excluded product: {'yes' if excluded else 'no'} - {mark(not excluded)}",
            "Top alternative: " + (f"{alt_name} scores {top_alt['total']:.2f} ({margin:+.2f} vs main, pivot "
                                   f"needs +{t['pivot_margin']}), {top_alt['signals']} own sourced signal(s)"
                                   if top_alt and margin is not None else "none"),
        ]
        evidence = {"sourced_signals": n, "domains": doms, "main_score": main_total, "hard_blocker": blocker or None,
                    "excluded": excluded, "top_alternative": alt_name, "alternative_margin": margin,
                    "repeats_done": repeats_done, "thresholds": dict(t), "mocked_evidence_counted": False,
                    "signals_from_fetched_pages": fetched, "signals_from_search_snippets": snippets,
                    "repeat_found_no_new_evidence": bool(no_new)}
        actual = f"{n} sourced signals from {len(doms)} domain(s); main score {score_text}/5"

        def decide(outcome, reasons, change):
            return GateDecision(outcome, reasons + ["Threshold check:"] + rows, evidence, "system", self.name,
                                self._target_text(), actual, change)

        stop = []
        if n < t["min_pain_signals"] or len(doms) < t["min_signal_domains"]:
            stop.append(f"Too little sourced pain evidence: {n} signal(s) from {len(doms)} domain(s); "
                        f"at least {t['min_pain_signals']} from {t['min_signal_domains']} domains are required.")
        if main_total is None or main_total < t["min_main_score"]:
            stop.append(f"The main idea scores {score_text}/5, below {t['min_main_score']}.")
        if blocker:
            stop.append(f"Hard founder-fit blocker: {blocker}")
        if excluded:
            stop.append("The idea is an excluded product.")
        if stop:
            return decide(Outcome.STOP, stop + ["Weak or missing evidence never passes this gate."], [
                f"At least {t['min_pain_signals']} distinct pain signals (complaints, questions, paid "
                f"workarounds) from {t['min_signal_domains']}+ different sites, each with a retrieved URL.",
                f"A main-idea score of {t['min_main_score']}/5 or more (better founder fit, or sourced pain).",
                "Real interview notes (--interviews) showing the problem in people's own words."])
        if (top_alt and margin is not None and margin >= t["pivot_margin"]
                and top_alt["signals"] >= t["pivot_min_alt_signals"]):
            # Applied by the orchestrator only if the run really pivots (the founder may override).
            ctx.options["pivot_candidate"] = {
                "pivot_idea": top_alt["name"],
                "phase1_runs_before_pivot": sum(1 for r in ctx.results if (r.phase, r.step) == (1, 1))}
            return decide(Outcome.PIVOT, [
                f"The alternative '{alt_name}' scores {top_alt['total']:.2f}, {margin:.2f} above the main idea "
                f"({score_text}), and has {top_alt['signals']} sourced signal(s) of its own. Phase 1 is rerun "
                "on that alternative."],
                ["A main idea that scores within the pivot margin of the best alternative."])
        borderline = n <= t["borderline_max_signals"] or main_total <= t["borderline_max_score"]
        if borderline and repeats_done < t["max_repeats"]:
            return decide(Outcome.REPEAT, [
                f"The evidence is borderline ({n} signals, score {score_text}). Step 1.1 is repeated once "
                "with new search queries."],
                [f"More than {t['borderline_max_signals']} sourced signals and a score above "
                 f"{t['borderline_max_score']}."])
        risks = ["Open risk: every signal comes from public web text, not from talking to customers.",
                 "Real interviews are needed before the Phase 2 gate.",
                 "Open risk: the sub-scores other than pain evidence are model judgements (ASSUMPTION)."]
        if borderline:
            risks.insert(0, f"Open risk: the evidence is still borderline after {repeats_done} repeat(s) "
                            f"({n} signals, score {score_text}).")
        if no_new:
            risks.insert(0, f"The REPEAT found no new sourced evidence ({prev_n} signal(s) before, {n} now).")
        return decide(Outcome.GO, ["The Phase 1 evidence thresholds are met."] + risks,
                      ["Interviews that contradict the web signals."])


# --- Phase 2 demand gate (M3) --------------------------------------------------

def load_phase2_thresholds() -> dict:
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parent / "config" / "thresholds.json"
    return json.loads(path.read_text(encoding="utf-8"))["phase2"]


MOCKED_EVIDENCE = "MOCKED evidence — demonstration only"
NOT_RUN_YET = "demand test not run yet"


def _logged_target(ctx):
    """The last target the founder approved, as written in the run's decision log, or None."""
    try:
        entries = ctx.logger.entries()
    except Exception:
        return None
    found = [e for e in entries if isinstance(e, dict) and e.get("action") == "register_experiment_target"]
    return found[-1] if found else None


class Phase2DemandGate(Gate):
    """After step 2.4. Judges the demand-test evidence against the target the founder approved
    (and hashed) in step 2.2, before any result existed.

    GO: sign-ups >= target AND conversion >= minimum. REPEAT (once, with a changed channel or
    message), then PIVOT: 50-99% of the target, or the target met with too low / unmeasured
    conversion. STOP below 50%, or PIVOT to Phase 1's top alternative when it scored well.
    Missing evidence, a missing target or a changed target is STOP, never GO. MOCKED evidence
    is labelled on every outcome.
    """
    name = "validate"

    def __init__(self, thresholds: dict | None = None):
        self.t = dict(thresholds or load_phase2_thresholds())

    def describe_target(self) -> dict:
        t = self.t
        return {"gate": self.name, "thresholds": dict(t), "target_text": (
            f"the founder-approved, SHA-256-registered target from step 2.2 (default: >= {t['target_signups']} "
            f"waitlist sign-ups in {t['window_days']} days and >= {t['min_conversion']:.0%} conversion); "
            f"50-99% -> REPEAT once, then PIVOT; < 50% -> STOP (or PIVOT to a well-scored Phase 1 alternative)")}

    def evaluate(self, result, ctx, attempts_remaining: int = 0) -> GateDecision:
        from agents.experimenter import target_hash
        t = self.t
        data = (getattr(result, "data", None) or {})
        reg = ctx.options.get("experiment_target")
        ev = data.get("evidence") or {}
        mocked = bool(ev.get("mocked"))
        run_id = getattr(ctx, "run_id", "<run_id>")
        evidence = {"evidence_type": ev.get("type"), "signups": ev.get("signups"), "visitors": ev.get("visitors"),
                    "conversion": ev.get("conversion"), "days_elapsed": ev.get("days_elapsed"), "mocked": mocked,
                    "target": None, "target_sha256": None, "approved_at": None}

        def decide(outcome, reasons, target_text, actual, change):
            d = GateDecision(outcome, list(reasons), evidence, "system", self.name, target_text, actual, change)
            if mocked:
                d.basis = "MOCKED"
                d.reasons.append(f"{MOCKED_EVIDENCE}: the sign-up numbers were typed in for a demo. "
                                 "This is not a real validation.")
            return d

        rerun = (f"rerun with `--resume {run_id} --from phase2 --signups N --visitors V` "
                 "(or `--waitlist export.csv`)")
        if not isinstance(reg, dict) or not reg.get("approved_at"):
            return decide(Outcome.STOP, ["No pre-registered target: the founder did not approve a demand-test "
                                         "target before results existed, so no result can be judged.",
                                         "Missing evidence never passes this gate."],
                          "none approved", "not judged", ["A demand test whose target the founder approved in "
                                                          "step 2.2 before any result existed."])
        target = reg["target"]
        evidence.update(target=target, target_sha256=reg.get("sha256"), approved_at=reg.get("approved_at"))
        target_text = (f">= {target['signups']} waitlist sign-ups in {target['window_days']} days and >= "
                       f"{target['min_conversion']:.0%} conversion (approved by the founder at {reg['approved_at']}; "
                       f"SHA-256 {str(reg.get('sha256', ''))[:16]}...)")
        if target_hash(target) != reg.get("sha256"):
            return decide(Outcome.STOP, ["The registered target does not match its SHA-256 hash: it was changed "
                                         "after approval. The result cannot be judged."], target_text,
                          "not judged (target changed)", ["A new experiment (REPEAT of step 2.2) with a newly "
                                                          "approved target."])
        logged = _logged_target(ctx)
        if logged is not None and (logged.get("sha256") != reg.get("sha256") or logged.get("target") != target):
            return decide(Outcome.STOP, ["The stored target does not match the target the founder approved, as "
                                         "recorded in the run's decision log (register_experiment_target). "
                                         "The result cannot be judged."], target_text,
                          "not judged (target differs from the logged one)",
                          ["A new experiment (REPEAT of step 2.2) with a newly approved target."])
        if ev.get("type") == "waitlist_unreadable":
            return decide(Outcome.STOP, [f"The --waitlist export could not be read ({ev.get('waitlist_error')}); "
                                         "no other evidence was used.",
                                         f"{NOT_RUN_YET} — {rerun}.",
                                         "No usable sign-up data was supplied, and missing evidence is never GO."],
                          target_text, "no evidence (waitlist export unreadable)", [f"Real waitlist numbers: {rerun}."])
        if ev.get("type") in (None, "none") or ev.get("signups") is None:
            return decide(Outcome.STOP, [f"{NOT_RUN_YET} — {rerun}.",
                                         "No sign-up data was supplied, and missing evidence is never GO."],
                          target_text, "no evidence", [f"Real waitlist numbers: {rerun}."])

        signups, conv = ev["signups"], ev.get("conversion")
        share = signups / target["signups"] if target["signups"] else 0
        conv_text = "not measured (no visitor count)" if conv is None else f"{conv:.1%}"
        label = MOCKED_EVIDENCE if mocked else {"waitlist_csv": "waitlist export (CSV, aggregate counts only)",
                                                "founder_reported": "founder-reported"}.get(ev["type"], ev["type"])
        visitors = ev.get("visitors")
        actual = (f"{signups} sign-ups ({share:.0%} of the target); visitors "
                  f"{visitors if visitors is not None else 'unknown'}; conversion {conv_text}; "
                  f"day {ev.get('days_elapsed', '?')} of {target['window_days']}; evidence: {label}")
        conv_ok = conv is not None and conv >= target["min_conversion"]
        # REPEATs already used: counted in this process, and persisted across --resume by the
        # orchestrator (``phase2_repeats_used``), so a resume never grants a fresh REPEAT.
        repeats_done = max(0, sum(1 for r in ctx.results if (r.phase, r.step) == (2, 4)) - 1,
                           int(ctx.options.get("phase2_repeats_used") or 0))
        evidence["repeats_done"] = repeats_done
        change_go = [f"At least {target['signups']} sign-ups with conversion >= {target['min_conversion']:.0%} "
                     f"within {target['window_days']} days, from real (not MOCKED) waitlist data."]

        if share >= 1 and conv_ok:
            return decide(Outcome.GO, [f"Target met: {signups} sign-ups against {target['signups']}, conversion "
                                       f"{conv_text} against {target['min_conversion']:.0%}."], target_text, actual,
                          ["Sign-ups below the target, or conversion below the minimum."])
        if share >= t["repeat_band_min"]:
            why = (f"{signups} sign-ups is {share:.0%} of the target" if share < 1 else
                   f"the sign-up target was reached, but conversion is {conv_text}, below "
                   f"{target['min_conversion']:.0%} or not measured")
            if repeats_done < t["max_repeats"]:
                ctx.options["experiment_change_note"] = (
                    f"The previous demand test missed: {why}. Change the channel or the message.")
                return decide(Outcome.REPEAT, [f"Target NOT met: {why}.",
                                               "The demand test is repeated ONCE with a changed channel or message "
                                               "(a new experiment, approved again before results)."],
                              target_text, actual, change_go)
            return decide(Outcome.PIVOT, [f"Target NOT met: {why}.",
                                          f"The test was already repeated {repeats_done} time(s), so this is a PIVOT."],
                          target_text, actual, change_go)
        reasons = [f"Target NOT met: {signups} sign-ups is {share:.0%} of the target of {target['signups']} "
                   f"(less than {t['repeat_band_min']:.0%}). The demand evidence is weak."]
        alt = self._strong_alternative(ctx)
        if alt:
            reasons.append(f"Phase 1's top alternative '{alt['name']}' scored {alt['total']:.2f}/5 with "
                           f"{alt['signals']} sourced signal(s): PIVOT to it (a new Phase 1 run on that idea).")
            return decide(Outcome.PIVOT, reasons, target_text, actual, change_go)
        reasons.append("No Phase 1 alternative scored well enough to pivot to, so the recommendation is STOP.")
        return decide(Outcome.STOP, reasons, target_text, actual, change_go)

    def _strong_alternative(self, ctx):
        scoring = ctx.latest(1, 2)
        ideas = ((scoring.data if scoring else {}) or {}).get("ideas") or []
        alts = [x for x in ideas if x.get("id") != "main" and isinstance(x.get("total"), (int, float))]
        best = max(alts, key=lambda x: x["total"], default=None)
        if best and best["total"] >= self.t["pivot_alt_min_score"] and \
                best.get("signals", 0) >= self.t["pivot_min_alt_signals"]:
            return {"name": best.get("short") or best.get("name"), "total": best["total"],
                    "signals": best.get("signals", 0)}
        return None
