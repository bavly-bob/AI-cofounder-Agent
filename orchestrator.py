"""Orchestrator: runs the steps in order, with a gate after each one.

Per step: guardrail check -> run the step -> output check -> save the step file
-> run each gate -> save the gate file -> founder confirms or overrides -> act.

    python orchestrator.py --brief examples/briefs/weak_idea.json --metric 12

Normal runs write to ``runs/<run_id>/`` (git-ignored). Only ``--demo`` writes
to ``outputs/`` and the root ``decision_log.json``.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import guardrails
from approval import FOUNDER, CLIApprover, DenyAllApprover
from brief import BriefError, FounderBrief, load_brief, parse_brief_text
from decision_log import DecisionLogger, is_http_url, new_run_id
from gates import BASIS_LABEL, FinalDecision, Outcome, founder_review, outcome_label, render_decision_md
from steps import (OUTPUT_NAME_RE, RunContext, header_line, output_filename, registered_steps,
                   render_step_markdown)

ROOT = Path(__file__).resolve().parent
MAX_REPEATS = 2   # a step is rerun at most twice (three attempts in total)
MAX_PIVOTS = 1    # a run goes back to an earlier step at most once


@dataclass
class RunSummary:
    run_id: str
    status: str = "running"            # "completed" | "stopped" | "error"
    final_outcome: str | None = None   # "go" | "pivot" | "stop"
    output_dir: Path | None = None
    log_path: Path | None = None
    steps: list = field(default_factory=list)   # one dict per step attempt
    gates: list = field(default_factory=list)   # one dict per gate decision
    files: list = field(default_factory=list)   # file names written, in order
    timeline: list = field(default_factory=list)  # ("step" | "gate", row dict), in order
    total_cost: float = 0
    total_tokens: int = 0
    stop_reasons: list = field(default_factory=list)
    safe_alternative: str | None = None


def build_gate_plan(steps) -> dict:
    """{(phase, step): [gates]} from each step's optional ``default_gates()``."""
    plan = {}
    for step in steps:
        make = getattr(step, "default_gates", None)
        plan[(step.phase, step.step)] = list(make()) if callable(make) else []
    return plan


def _guard(check, text):
    """Run a guardrail check. If the check itself breaks, fail closed (blocked)."""
    try:
        return check(text)
    except Exception as e:
        return SimpleNamespace(allowed=False, category=None,
                               reason=f"The guardrail check could not be completed ({e}), so this was blocked.",
                               safe_alternative=None)


def _save(ctx: RunContext, summary: RunSummary, filename: str, text: str) -> str:
    if not OUTPUT_NAME_RE.match(filename):
        raise ValueError(f"output file name does not follow the naming rule: {filename}")
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    (ctx.output_dir / filename).write_text(text.rstrip("\n") + "\n", encoding="utf-8", newline="\n")
    if filename not in summary.files:
        summary.files.append(filename)
    return filename


def _write_stop_memo(ctx, summary, phase, where, outcome_text, reasons, change_conditions,
                     evidence=None, safe_alternative=None, overridable=True, status="WORKING") -> str:
    lines = [
        header_line(ctx.run_id, None, status),
        "",
        f"# Stop memo: phase {phase}",
        "",
        f"**The run was halted {where}. Outcome: {outcome_text}.**",
        "",
        "## Why",
        "",
        *[f"- {r}" for r in reasons],
        "",
    ]
    if evidence:
        lines += ["## Evidence", "", *[f"- {k}: {v}" for k, v in evidence.items()], ""]
    if safe_alternative:
        lines += ["## Suggested compliant alternative", "", f"- {safe_alternative}", ""]
    lines += ["## What evidence would change this decision", "",
              *[f"- {c}" for c in (change_conditions or ["A new run with stronger, sourced evidence."])], "",
              "## Founder options", "",
              "- Start a new run with a changed brief or new evidence."]
    if overridable:
        lines.append("- Override a gate recommendation at the gate prompt (logged with both outcomes).")
    else:
        lines.append("- This outcome cannot be changed at a gate prompt.")
    lines.append("")
    return _save(ctx, summary, output_filename(phase, "gate", "stop_memo"), "\n".join(lines))


def run_pipeline(brief: FounderBrief, steps=None, gates=None, approver=None, demo: bool = False, *,
                 metric: float | None = None, base_dir=None, run_id: str | None = None,
                 echo=None) -> RunSummary:
    """Run the pipeline and return a RunSummary. Does not raise for a failing step or gate.

    ``steps`` defaults to the registered steps and ``gates`` to their default
    gates ({(phase, step): [gate, ...]}). ``approver`` defaults to DenyAllApprover.
    ``base_dir`` moves ``runs/`` (or, with demo, ``outputs/`` and the log) elsewhere.
    """
    echo = echo or (lambda *_: None)
    steps = list(steps) if steps is not None else registered_steps()
    gates = gates if gates is not None else build_gate_plan(steps)
    approver = approver or DenyAllApprover()
    base = Path(base_dir) if base_dir is not None else ROOT
    run_id = run_id or new_run_id()
    if demo:
        out_dir, log_path = base / "outputs", base / "decision_log.json"
    else:
        out_dir = base / "runs" / run_id
        log_path = out_dir / "decision_log.json"
    logger = DecisionLogger(log_path, run_id=run_id, reset=demo)
    ctx = RunContext(brief=brief, run_id=run_id, logger=logger, approver=approver, output_dir=out_dir,
                     options={"metric": metric})
    summary = RunSummary(run_id=run_id, output_dir=out_dir, log_path=log_path)
    logger.log("orchestrator", "start_run", demo=bool(demo),
               steps=[f"{s.phase}.{s.step} {s.name}" for s in steps], approver_type=type(approver).__name__)

    def finish(status, outcome):
        summary.status, summary.final_outcome = status, outcome
        logger.log("orchestrator", "end_run", cost=summary.total_cost, status=status, outcome=outcome,
                   tokens=summary.total_tokens)
        return summary

    def halt(phase, where, outcome, reasons, change=None, evidence=None, alternative=None, status="stopped",
             overridable=True, basis="WORKING"):
        summary.stop_reasons = list(reasons)
        summary.safe_alternative = alternative
        memo = _write_stop_memo(ctx, summary, phase, where, outcome.value.upper(), reasons, change,
                                evidence, alternative, overridable, basis)
        logger.log("orchestrator", "write_stop_memo", outcome=outcome.value, phase=phase, output_file=memo)
        echo(f"  HALT ({outcome.value.upper()}): stop memo saved as {memo}")
        return finish(status, outcome.value)

    try:
        # The brief is checked before anything runs. An excluded product stops the run here.
        verdict = _guard(guardrails.check_brief, brief.as_text())
        if not verdict.allowed:
            logger.log("guardrail", "refuse_brief", outcome="stop", category=verdict.category,
                       reason=verdict.reason, safe_alternative=verdict.safe_alternative)
            echo(f"Brief refused ({verdict.category}): {verdict.reason}")
            return halt(0, "before any step ran, at the brief check", Outcome.STOP,
                        [f"The brief was refused by the guardrails (category: {verdict.category}).",
                         verdict.reason],
                        ["A brief for an allowed digital product (no physical goods; no financial, "
                         "investment or crypto products; no health or legal advice)."],
                        alternative=verdict.safe_alternative, overridable=False)
        logger.log("guardrail", "check_brief", outcome="allowed")

        index = {(s.phase, s.step): i for i, s in enumerate(steps)}
        attempts, file_runs, pivots, i = {}, {}, 0, 0
        while i < len(steps):
            step = steps[i]
            key = (step.phase, step.step)
            attempt = attempts[key] = attempts.get(key, 0) + 1
            file_no = file_runs[key] = file_runs.get(key, 0) + 1
            where = f"at step {step.phase}.{step.step} ({step.name})"
            tag = dict(phase=step.phase, step=step.step, attempt=attempt)
            step_gates = gates.get(key, [])

            # Targets are fixed and logged BEFORE the step runs.
            for gate in step_gates:
                logger.log("gate", "set_gate_target", **{**gate.describe_target(), **tag})

            # 1. guardrail check on what the step is about to do
            make_request = getattr(step, "request_text", None)
            request = make_request(ctx) if callable(make_request) else brief.as_text()
            verdict = _guard(guardrails.check_request, request)
            if not verdict.allowed:
                logger.log("guardrail", "refuse_request", outcome="stop", category=verdict.category,
                           reason=verdict.reason, safe_alternative=verdict.safe_alternative, **tag)
                return halt(step.phase, where, Outcome.STOP,
                            [f"The request was refused by the guardrails (category: {verdict.category}).",
                             verdict.reason],
                            ["A request that stays inside the project's honesty and safety rules."],
                            alternative=verdict.safe_alternative, overridable=False)

            # 2. run the step
            try:
                result = step.run(ctx)
            except Exception as e:
                logger.log(step.agent, "step_error", outcome="error", error=f"{type(e).__name__}: {e}", **tag)
                return halt(step.phase, where, Outcome.STOP, [f"The step failed with an error: {e}"],
                            ["A run in which this step completes."], status="error")
            cost = result.cost if isinstance(result.cost, (int, float)) and not isinstance(result.cost, bool) else 0
            summary.total_cost += cost
            summary.total_tokens += result.tokens or 0

            # 3. output check, before anything is saved
            rendered = render_step_markdown(result, run_id)
            verdict = _guard(guardrails.check_output, rendered)
            if not verdict.allowed:
                logger.log("guardrail", "refuse_output", model=result.model, cost=cost, outcome="blocked",
                           category=verdict.category, reason=verdict.reason,
                           safe_alternative=verdict.safe_alternative, agent_refused=result.agent, **tag)
                echo(f"  [{step.phase}.{step.step} {step.name}] output refused ({verdict.category}); not saved")
                if attempt <= MAX_REPEATS:
                    continue
                return halt(step.phase, where, Outcome.STOP,
                            [f"The step's output was refused by the guardrails on every attempt "
                             f"(category: {verdict.category}). It was not saved.", verdict.reason],
                            ["Output that stays inside the project's honesty and safety rules."],
                            alternative=verdict.safe_alternative, overridable=False)

            # 4. save the step file
            step_file = _save(ctx, summary, output_filename(step.phase, f"step{step.step}", result.name, file_no),
                              rendered)
            ctx.results.append(result)
            logger.log(result.agent, f"run_{result.name}",
                       sources=[c.source_url for c in result.claims if is_http_url(c.source_url)],
                       model=result.model, cost=cost, status=result.status, tokens=result.tokens,
                       output_file=step_file, **tag)
            summary.steps.append({"id": f"{step.phase}.{step.step}", "name": result.name, "agent": result.agent,
                                  "status": result.status, "attempt": attempt, "file": step_file})
            summary.timeline.append(("step", summary.steps[-1]))
            echo(f"  [{step.phase}.{step.step} {result.name}] {result.status} -> {step_file}")

            # 5-7. gates, in order; the first one that is not GO decides
            final, decision = FinalDecision(Outcome.GO, None, Outcome.GO, None), None
            for gate in step_gates:
                decision = gate.evaluate(result, ctx, attempts_remaining=MAX_REPEATS + 1 - attempt)
                if decision.reviewable and decision.basis not in BASIS_LABEL and result.status in BASIS_LABEL:
                    decision.basis = result.status  # a gate on a STUBBED/MOCKED step is labelled as such
                gate_file = output_filename(step.phase, "gate", gate.name, file_no)
                _save(ctx, summary, gate_file, render_decision_md(decision, run_id, step.phase, step.step,
                                                                 attempt=attempt))
                logger.log("gate", gate.name, outcome=decision.outcome.value,
                           approved_by=FOUNDER if decision.recommended_by == FOUNDER
                           and decision.evidence.get("approved") else None,
                           recommended_by=decision.recommended_by, basis=decision.basis,
                           decision=decision.evidence.get("decision"), reasons=decision.reasons,
                           evidence=decision.evidence, output_file=gate_file, **tag)
                echo(f"  [gate {gate.name}] {outcome_label(decision).replace(chr(0x2014), '-')} -> {gate_file}")
                final = founder_review(decision, ctx, step.phase, step.step)
                _save(ctx, summary, gate_file, render_decision_md(decision, run_id, step.phase, step.step,
                                                                 final, attempt))
                summary.gates.append({
                    "after": f"{step.phase}.{step.step}", "gate": gate.name, "attempt": attempt,
                    "system_outcome": final.system_outcome.value,
                    "founder_outcome": final.founder_outcome.value if final.founder_outcome else None,
                    "outcome": final.outcome.value, "file": gate_file, "basis": decision.basis,
                })
                summary.timeline.append(("gate", summary.gates[-1]))
                if final.founder_outcome and final.founder_outcome != final.system_outcome:
                    echo(f"  [gate {gate.name}] founder override: {final.outcome.value.upper()}")
                if final.outcome != Outcome.GO:
                    break

            # 8. act on the outcome
            outcome = final.outcome
            reasons = list(decision.reasons) if decision else []
            change = decision.change_conditions if decision else None
            evidence = {"target": decision.target, "actual": decision.actual} if decision else None
            if final.founder_outcome and final.founder_outcome != final.system_outcome:
                reasons.append(f"The founder overrode the system outcome "
                               f"{final.system_outcome.value.upper()} with {outcome.value.upper()}.")
            if outcome == Outcome.GO:
                i += 1
                continue
            if outcome == Outcome.REPEAT:
                if attempt <= MAX_REPEATS:
                    continue
                outcome = Outcome.PIVOT
                reasons.append(f"The step was already repeated {MAX_REPEATS} times, so REPEAT becomes PIVOT.")
            if outcome == Outcome.PIVOT:
                target = getattr(step, "pivot_to", None)
                target = tuple(target) if target else None
                if target in index and index[target] < i and pivots < MAX_PIVOTS:
                    pivots += 1
                    logger.log("orchestrator", "pivot", outcome="pivot", pivot_to=f"{target[0]}.{target[1]}", **tag)
                    echo(f"  PIVOT: back to step {target[0]}.{target[1]}")
                    for j in range(index[target], len(steps)):
                        attempts.pop((steps[j].phase, steps[j].step), None)
                    i = index[target]
                    continue
                reasons.append("There is no earlier step left to pivot back to, so the run halts here."
                               if not (target in index and index[target] < i)
                               else f"The run already pivoted {MAX_PIVOTS} time(s), so it halts here.")
                return halt(step.phase, where, Outcome.PIVOT, reasons, change, evidence,
                            overridable=decision.reviewable, basis=decision.basis)
            return halt(step.phase, where, Outcome.STOP, reasons, change, evidence,
                        overridable=decision.reviewable, basis=decision.basis)

        return finish("completed", "go")
    except Exception as e:  # last line of defence: report, never crash in front of the founder
        summary.stop_reasons = [f"Unexpected error: {type(e).__name__}: {e}"]
        try:
            return finish("error", "stop")
        except Exception:
            summary.status, summary.final_outcome = "error", "stop"
            return summary


# --- CLI ---------------------------------------------------------------------

def format_summary(summary: RunSummary) -> str:
    rows = [("Kind", "Where", "Name", "Attempt", "Result", "File")]
    for kind, item in summary.timeline:   # in the order things happened
        if kind == "step":
            rows.append(("step", item["id"], item["name"], str(item["attempt"]), item["status"], item["file"]))
            continue
        result = item["system_outcome"].upper()
        if item.get("basis") in BASIS_LABEL:
            result += f" ({item['basis']})"
        if item["founder_outcome"] and item["founder_outcome"] != item["system_outcome"]:
            result += f" -> founder: {item['founder_outcome'].upper()}"
        rows.append(("gate", item["after"], item["gate"], str(item["attempt"]), result, item["file"]))
    widths = [max(len(r[c]) for r in rows) for c in range(len(rows[0]))]
    line = lambda r: " | ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip()
    out = ["", "RUN SUMMARY", line(rows[0]), "-+-".join("-" * w for w in widths)]
    out += [line(r) for r in rows[1:]]
    if len(rows) == 1:
        out.append("(no step ran)")
    out += [
        "",
        f"Run id        : {summary.run_id}",
        f"Final outcome : {(summary.final_outcome or 'n/a').upper()} (status: {summary.status})",
        f"Total cost    : ${summary.total_cost:.2f}",
        f"Total tokens  : {summary.total_tokens}",
        f"Files in      : {summary.output_dir}",
        f"Decision log  : {summary.log_path}",
    ]
    if summary.stop_reasons:
        out += ["Why           :"] + [f"  - {r}" for r in summary.stop_reasons]
    if summary.safe_alternative:
        out += [f"Suggested alternative: {summary.safe_alternative}"]
    return "\n".join(out)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Run the venture pipeline for one founder brief.")
    parser.add_argument("--brief", required=True, help="path to a brief (.json), or a text file with free text")
    parser.add_argument("--metric", type=float, default=None,
                        help="MOCKED validation metric for the STUBBED validate step (e.g. 12)")
    parser.add_argument("--demo", action="store_true",
                        help="write to outputs/ and the root decision_log.json instead of runs/<run_id>/")
    args = parser.parse_args(argv)

    path = Path(args.brief)
    try:
        if path.suffix.lower() == ".json":
            brief = load_brief(path)
        else:
            brief = parse_brief_text(path.read_text(encoding="utf-8-sig"))
    except (BriefError, OSError, UnicodeDecodeError) as e:
        print(f"Could not load the brief: {e}", file=sys.stderr)
        return 2

    print(f"Brief: {brief.idea}")
    summary = run_pipeline(brief, approver=CLIApprover(), demo=args.demo, metric=args.metric, echo=print)
    print(format_summary(summary))
    return 1 if summary.status == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
