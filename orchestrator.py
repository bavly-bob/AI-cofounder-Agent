"""Orchestrator: runs the steps in order, with a gate after each one.

Per step: guardrail check -> run the step -> output check -> save the step file
-> run each gate -> save the gate file -> founder confirms or overrides -> act.

    python orchestrator.py --brief examples/briefs/weak_idea.json
    python orchestrator.py --brief examples/briefs/decent_idea.json --fast
    python orchestrator.py --brief examples/briefs/decent_idea.json --interviews notes.md --metric 120

Normal runs write to ``runs/<run_id>/`` (git-ignored). Only ``--demo`` writes
to ``outputs/`` and the root ``decision_log.json``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import agents  # noqa: F401  (registers the Phase 1 agents as steps 1.1-1.5)
import guardrails
from approval import FOUNDER, CLIApprover, DenyAllApprover
from brief import BriefError, FounderBrief, load_brief, parse_brief_text
from decision_log import DecisionLogger, is_http_url, new_run_id
from llm import LLMError
from gates import BASIS_LABEL, FinalDecision, Outcome, founder_review, outcome_label, render_decision_md
from steps import (EXTRA_OUTPUT_NAME_RE, OUTPUT_NAME_RE, RunContext, header_line, output_filename,
                   registered_steps, render_step_markdown, result_from_dict, result_to_dict)
from tools.research import Research, budgets

ROOT = Path(__file__).resolve().parent
MAX_REPEATS = 2   # a step is rerun at most twice (three attempts in total)
MAX_PIVOTS = 1    # a run goes back to an earlier step at most once
RESUME_DIR = "_resume"   # resume data: runs/_resume/<run_id>.json (run_state.py owns runs/<run_id>/state.json)
MOCKED_EVIDENCE = "MOCKED evidence — demonstration only"
_STATE_OPTIONS = ("experiment_target", "pivot_idea", "phase1_runs_before_pivot")


class ResumeError(ValueError):
    """The run to resume is missing or its saved state cannot be used."""


def load_state(base, run_id: str) -> dict:
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$", str(run_id or "")):
        raise ResumeError(f"not a valid run id: {run_id!r}")
    path = Path(base) / "runs" / RESUME_DIR / f"{run_id}.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ResumeError(f"no saved run {run_id!r} under runs/ (or it was a --demo run)") from None
    except (OSError, ValueError) as e:
        raise ResumeError(f"the saved state of run {run_id!r} cannot be read: {e}") from None
    if not isinstance(state, dict) or not isinstance(state.get("results"), list):
        raise ResumeError(f"the saved state of run {run_id!r} is not usable")
    return state


def _idea_is_empty(idea) -> bool:
    """Fewer than 3 letters (in any script): there is nothing to research."""
    return len(re.findall(r"[^\W\d_]", str(idea or ""))) < 3


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
    seconds: float = 0                 # wall-clock time of the run
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


def _save_extra(ctx: RunContext, summary: RunSummary, filename: str, text: str) -> str:
    if not EXTRA_OUTPUT_NAME_RE.match(filename):
        raise ValueError(f"extra output file name does not follow the naming rule: {filename}")
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    (ctx.output_dir / filename).write_text(text.rstrip("\n") + "\n", encoding="utf-8", newline="\n")
    if filename not in summary.files:
        summary.files.append(filename)
    return filename


def _attempt_name(filename: str, file_no: int) -> str:
    if file_no <= 1:
        return filename
    stem, dot, ext = filename.rpartition(".")
    return f"{stem}_attempt{file_no}.{ext}"


def _write_stop_memo(ctx, summary, phase, where, outcome_text, reasons, change_conditions,
                     evidence=None, safe_alternative=None, overridable=True, status="WORKING",
                     keep_existing: bool = False) -> str:
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
    name, n = output_filename(phase, "gate", "stop_memo"), 1
    while keep_existing and (ctx.output_dir / name).exists():   # a resumed run keeps the earlier memo
        n += 1
        name = output_filename(phase, "gate", "stop_memo", n)
    return _save(ctx, summary, name, "\n".join(lines))


def run_pipeline(brief: FounderBrief, steps=None, gates=None, approver=None, demo: bool = False, *,
                 metric: float | None = None, base_dir=None, run_id: str | None = None,
                 echo=None, llm_client=None, search_fn=None, fetch_fn=None, fast: bool = False,
                 interviews_path=None, phases=None, resume_run_id: str | None = None, from_phase=None,
                 waitlist_path=None, signups=None, visitors=None, mock_signups=None,
                 mock_visitors=None) -> RunSummary:
    """Run the pipeline and return a RunSummary. Does not raise for a failing step or gate.

    ``steps`` defaults to the registered steps and ``gates`` to their default
    gates ({(phase, step): [gate, ...]}). ``approver`` defaults to DenyAllApprover.
    ``base_dir`` moves ``runs/`` (or, with demo, ``outputs/`` and the log) elsewhere.
    ``llm_client``, ``search_fn`` and ``fetch_fn`` are injected dependencies (default: the local
    Ollama model and the free web tools). ``fast`` uses the smaller research budget.
    Web sources are cached under ``runs/<run_id>/sources/``.

    ``phases`` (e.g. {1, 2}) limits the run to those phases. ``resume_run_id`` with
    ``from_phase=2`` reuses the saved Phase 1 results of that run (and the Phase 2 steps that
    already passed their gate, e.g. the approved target), and continues with Phase 2.
    Phase 2 demand evidence, in priority order: ``waitlist_path`` (CSV export; only counts
    are kept), ``signups``/``visitors`` (founder-reported), ``mock_signups``/``mock_visitors``
    (MOCKED, demo only). Without any, the Phase 2 gate is STOP ("demand test not run yet").
    """
    started_at = time.monotonic()
    echo = echo or (lambda *_: None)
    approver = approver or DenyAllApprover()
    base = Path(base_dir) if base_dir is not None else ROOT
    state = None
    if resume_run_id:
        state = load_state(base, resume_run_id)   # raises ResumeError: the CLI reports it plainly
        run_id = resume_run_id
        if brief is None:
            brief = FounderBrief.from_dict(state.get("brief") or {})
        from_phase = int(str(from_phase or 2).lower().replace("phase", "") or 2)
    steps = list(steps) if steps is not None else registered_steps()
    if phases:
        wanted = {int(p) for p in phases}
        steps = [s for s in steps if s.phase in wanted]
    if state is not None:
        steps = [s for s in steps if s.phase >= from_phase]
    gates = gates if gates is not None else build_gate_plan(steps)
    run_id = run_id or new_run_id()
    if demo:
        out_dir, log_path = base / "outputs", base / "decision_log.json"
    else:
        out_dir = base / "runs" / run_id
        log_path = out_dir / "decision_log.json"
    run_dir = base / "runs" / run_id
    logger = DecisionLogger(log_path, run_id=run_id, reset=bool(demo) and state is None)
    plan = budgets(bool(fast))
    research = Research(run_id, logger, run_dir / "sources", search_fn, fetch_fn,
                        plan["max_searches"], plan["max_fetches"], echo)
    ctx = RunContext(brief=brief, run_id=run_id, logger=logger, approver=approver, output_dir=out_dir,
                     options={"metric": metric, "fast": bool(fast), "research_plan": plan,
                              "interviews_path": str(interviews_path) if interviews_path else None,
                              "run_dir": str(run_dir),
                              "waitlist_path": str(waitlist_path) if waitlist_path else None,
                              "signups": signups, "visitors": visitors,
                              "mock_signups": mock_signups, "mock_visitors": mock_visitors},
                     llm_client=llm_client, research=research, echo=echo)
    summary = RunSummary(run_id=run_id, output_dir=out_dir, log_path=log_path)
    evidence_mocked = (mock_signups is not None and not waitlist_path and signups is None)
    if metric is None and evidence_mocked:
        ctx.options["metric"] = float(mock_signups)   # the M1 STUBBED validate step reads its MOCKED value here
    logger.log("orchestrator", "start_run", demo=bool(demo),
               steps=[f"{s.phase}.{s.step} {s.name}" for s in steps], approver_type=type(approver).__name__,
               resumed_from=resume_run_id, phases=sorted({s.phase for s in steps}))
    gate_finals = dict((state or {}).get("gate_finals") or {})   # "phase.step" -> final outcome
    file_runs = {tuple(int(x) for x in k.split(".")): v for k, v in ((state or {}).get("file_runs") or {}).items()}

    def save_state():
        if demo:   # a --demo run writes only to outputs/ and the root log; it cannot be resumed
            return
        try:
            resume_dir = base / "runs" / RESUME_DIR
            resume_dir.mkdir(parents=True, exist_ok=True)
            data = {"version": 1, "run_id": run_id, "brief": brief.to_dict() if hasattr(brief, "to_dict") else {},
                    "results": [result_to_dict(r) for r in ctx.results], "gate_finals": gate_finals,
                    "file_runs": {f"{k[0]}.{k[1]}": v for k, v in file_runs.items()},
                    "options": {k: ctx.options.get(k) for k in _STATE_OPTIONS if ctx.options.get(k) is not None},
                    "sources": research.registry.entries(), "status": summary.status,
                    "final_outcome": summary.final_outcome}
            tmp = resume_dir / f".{run_id}.json.tmp"
            tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(tmp, resume_dir / f"{run_id}.json")
        except (OSError, TypeError, ValueError) as e:
            logger.log("orchestrator", "save_state_failed", error=str(e)[:300])

    def finish(status, outcome):
        summary.status, summary.final_outcome = status, outcome
        summary.seconds = round(time.monotonic() - started_at, 1)
        logger.log("orchestrator", "end_run", cost=summary.total_cost, status=status, outcome=outcome,
                   tokens=summary.total_tokens, seconds=summary.seconds)
        save_state()
        return summary

    def halt(phase, where, outcome, reasons, change=None, evidence=None, alternative=None, status="stopped",
             overridable=True, basis="WORKING"):
        summary.stop_reasons = list(reasons)
        summary.safe_alternative = alternative
        memo = _write_stop_memo(ctx, summary, phase, where, outcome.value.upper(), reasons, change,
                                evidence, alternative, overridable, basis, keep_existing=state is not None)
        logger.log("orchestrator", "write_stop_memo", outcome=outcome.value, phase=phase, output_file=memo)
        echo(f"  HALT ({outcome.value.upper()}): stop memo saved as {memo}")
        return finish(status, outcome.value)

    try:
        # An empty or near-empty idea: ask for more information before any model or web call.
        if _idea_is_empty(getattr(brief, "idea", "")):
            logger.log("orchestrator", "need_more_info", outcome="stop",
                       reason="the brief has no usable idea (fewer than 3 letters)")
            echo("The brief has no usable idea. Please describe the idea in a sentence or two.")
            return halt(0, "before any step ran, at the brief check", Outcome.STOP,
                        ["Please describe the idea: the brief has no usable idea text, so there is nothing to "
                         "research yet (a request for more information, not a judgement of the idea)."],
                        ["A brief whose idea says, in a sentence or two, what the product is and who it is for."])

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
        attempts, pivots, i = {}, 0, 0
        phases_started = set()

        if state is not None:
            # Resume: restore the saved results and sources, then start at the first Phase 2 step
            # that has not passed its gate (the demand results step is always rerun).
            for entry in state.get("sources") or []:
                research.registry.add(entry.get("url"), entry.get("title", ""), entry.get("snippet", ""),
                                      "search_result", entry.get("provider", ""), entry.get("query", ""))
                if entry.get("text"):
                    research.registry.add(entry.get("url"), entry.get("title", ""), entry["text"], "fetched")
            for key, value in (state.get("options") or {}).items():
                if key in _STATE_OPTIONS:
                    ctx.options[key] = value
            saved = [result_from_dict(r) for r in state["results"]]
            ctx.results.extend(r for r in saved if r.phase < from_phase)
            phase1_final = gate_finals.get("1.4")
            logger.log("orchestrator", "resume_run", outcome=None, from_phase=from_phase,
                       restored_results=len(ctx.results), restored_sources=len(research.registry),
                       phase1_gate=phase1_final)
            for j, step in enumerate(steps):
                key = (step.phase, step.step)
                latest = next((r for r in reversed(saved) if (r.phase, r.step) == key), None)
                if key != (2, 4) and latest is not None and gate_finals.get(f"{key[0]}.{key[1]}") == "go":
                    ctx.results.append(latest)
                    logger.log("orchestrator", "reuse_saved_step", phase=key[0], step=key[1], name=step.name)
                    echo(f"  [{key[0]}.{key[1]} {step.name}] reused from the saved run (it passed its gate)")
                    i = j + 1
                    continue
                break
            if from_phase == 2 and phase1_final != "go":
                logger.log("orchestrator", "phase2_entry_check", outcome="stop", phase1_gate=phase1_final)
                return halt(2, "at the start of Phase 2", Outcome.STOP,
                            [f"Phase 2 starts only after a Phase 1 GO (or a founder override to GO). The saved "
                             f"Phase 1 gate outcome is {str(phase1_final).upper()}."],
                            ["A Phase 1 run whose evidence gate ends in GO."], overridable=False)

        while i < len(steps):
            step = steps[i]
            key = (step.phase, step.step)
            attempt = attempts[key] = attempts.get(key, 0) + 1
            file_no = file_runs[key] = file_runs.get(key, 0) + 1
            where = f"at step {step.phase}.{step.step} ({step.name})"
            tag = dict(phase=step.phase, step=step.step, attempt=attempt)
            step_gates = gates.get(key, [])

            # At the start of a phase, every gate of that phase logs its thresholds.
            if step.phase not in phases_started:
                phases_started.add(step.phase)
                echo(f"Phase {step.phase}")
                if step.phase == 1:
                    logger.log("orchestrator", "set_research_budget", phase=1, **{
                        k: v for k, v in plan.items() if not k.startswith("_")})
                if step.phase == 2:
                    discover = [g for g in summary.gates if g["gate"] == "discover"]
                    if discover or state is not None:
                        last = discover[-1] if discover else None
                        logger.log("orchestrator", "phase2_entry_check", outcome="go",
                                   phase1_gate=last["outcome"] if last else gate_finals.get("1.4"),
                                   phase1_system_outcome=last["system_outcome"] if last else None,
                                   founder_override=bool(last and last["founder_outcome"]
                                                         and last["founder_outcome"] != last["system_outcome"]),
                                   evidence_mocked=evidence_mocked)
                for other in steps:
                    if other.phase == step.phase:
                        for gate in gates.get((other.phase, other.step), []):
                            logger.log("gate", "set_phase_thresholds", logged_at="phase_start",
                                       gate_after=f"{other.phase}.{other.step}", phase=step.phase,
                                       **{k: v for k, v in gate.describe_target().items() if k != "phase"})

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
            echo(f"  [{step.phase}.{step.step} {step.name}] running ({step.agent})...")
            started = time.monotonic()
            try:
                result = step.run(ctx)
            except Exception as e:
                seconds = round(time.monotonic() - started, 1)
                logger.log(step.agent, "step_error", outcome="error", error=f"{type(e).__name__}: {e}",
                           seconds=seconds, **tag)
                summary.steps.append({"id": f"{step.phase}.{step.step}", "name": step.name, "agent": step.agent,
                                      "status": "ERROR", "attempt": attempt, "file": "-", "seconds": seconds,
                                      "tokens": 0})
                summary.timeline.append(("step", summary.steps[-1]))
                if isinstance(e, LLMError):   # the local model is unavailable: a clean STOP, not a crash
                    echo(f"  [{step.phase}.{step.step} {step.name}] FAILED after {seconds}s: the local model "
                         "could not be used (details in the decision log)")
                    return halt(step.phase, where, Outcome.STOP,
                                ["The local model could not be used, so this step could not run. Check that "
                                 "Ollama is running and the model is pulled (see the README); the technical "
                                 "details are in the decision log.",
                                 "Nothing was decided without evidence."],
                                ["A run with Ollama running and the model pulled (see the README)."])
                echo(f"  [{step.phase}.{step.step} {step.name}] FAILED after {seconds}s: {e}")
                return halt(step.phase, where, Outcome.STOP, [f"The step failed with an error: {e}"],
                            ["A run in which this step completes."], status="error")
            seconds = round(time.monotonic() - started, 1)
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

            # 4. save the step file (and any extra text file the step produced, e.g. a landing page)
            step_file = _save(ctx, summary, output_filename(step.phase, f"step{step.step}", result.name, file_no),
                              rendered)
            extras = (result.data or {}).get("extra_files") or {}
            extra_files = [_save_extra(ctx, summary, _attempt_name(name, file_no), text)
                           for name, text in extras.items()]
            ctx.results.append(result)
            logger.log(result.agent, f"run_{result.name}",
                       sources=[c.source_url for c in result.claims if is_http_url(c.source_url)],
                       model=result.model, cost=cost, status=result.status, tokens=result.tokens,
                       output_file=step_file, extra_files=extra_files or None, seconds=seconds, **tag)
            summary.steps.append({"id": f"{step.phase}.{step.step}", "name": result.name, "agent": result.agent,
                                  "status": result.status, "attempt": attempt, "file": step_file,
                                  "seconds": seconds, "tokens": result.tokens or 0})
            summary.timeline.append(("step", summary.steps[-1]))
            echo(f"  [{step.phase}.{step.step} {result.name}] {result.status} in {seconds}s"
                 f" ({result.tokens or 0} tokens) -> {step_file}" + "".join(f", {x}" for x in extra_files))
            save_state()

            # 5-7. gates, in order; the first one that is not GO decides
            final, decision = FinalDecision(Outcome.GO, None, Outcome.GO, None), None
            for gate in step_gates:
                decision = gate.evaluate(result, ctx, attempts_remaining=MAX_REPEATS + 1 - attempt)
                if decision.reviewable and decision.basis not in BASIS_LABEL and result.status in BASIS_LABEL:
                    decision.basis = result.status  # a gate on a STUBBED/MOCKED step is labelled as such
                if evidence_mocked and step.phase == 2 and not any(MOCKED_EVIDENCE in r for r in decision.reasons):
                    decision.reasons.append(f"{MOCKED_EVIDENCE}: this run's sign-up numbers are typed in "
                                            "(--mock-signups), so nothing in it is a real validation.")
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
                if decision.evidence.get("repeat_found_no_new_evidence"):
                    echo(f"  [gate {gate.name}] note: the REPEAT found no new sourced evidence")
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
            gate_finals[f"{key[0]}.{key[1]}"] = final.outcome.value
            save_state()

            # 8. act on the outcome
            outcome = final.outcome
            candidate = ctx.options.pop("pivot_candidate", None)   # set by a gate that recommended PIVOT
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
                back = getattr(step, "repeat_from", None)
                back = tuple(back) if back else None
                if back in index and index[back] < i and attempt <= MAX_REPEATS:
                    logger.log("orchestrator", "repeat_from", outcome="repeat",
                               repeat_from=f"{back[0]}.{back[1]}", **tag)
                    echo(f"  REPEAT: back to step {back[0]}.{back[1]}")
                    i = index[back]
                    continue
                if attempt <= MAX_REPEATS:
                    continue
                outcome = Outcome.PIVOT
                reasons.append(f"The step was already repeated {MAX_REPEATS} times, so REPEAT becomes PIVOT.")
            if outcome == Outcome.PIVOT:
                target = getattr(step, "pivot_to", None)
                target = tuple(target) if target else None
                if target in index and index[target] < i and pivots < MAX_PIVOTS:
                    pivots += 1
                    if isinstance(candidate, dict):
                        ctx.options.update(candidate)
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
    rows = [("Kind", "Where", "Name", "Attempt", "Result", "Time", "File")]
    for kind, item in summary.timeline:   # in the order things happened
        if kind == "step":
            rows.append(("step", item["id"], item["name"], str(item["attempt"]), item["status"],
                         f"{item.get('seconds', 0):.1f}s", item["file"]))
            continue
        result = item["system_outcome"].upper()
        if item.get("basis") in BASIS_LABEL:
            result += f" ({item['basis']})"
        if item["founder_outcome"] and item["founder_outcome"] != item["system_outcome"]:
            result += f" -> founder: {item['founder_outcome'].upper()}"
        rows.append(("gate", item["after"], item["gate"], str(item["attempt"]), result, "", item["file"]))
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
        f"Step time     : {sum(s.get('seconds', 0) for s in summary.steps):.1f}s",
        f"Total time    : {summary.seconds:.1f}s",
        f"Files in      : {summary.output_dir}",
        f"Decision log  : {summary.log_path}",
    ]
    if summary.stop_reasons:
        out += ["Why           :"] + [f"  - {r}" for r in summary.stop_reasons]
    if summary.safe_alternative:
        out += [f"Suggested alternative: {summary.safe_alternative}"]
    return "\n".join(out)


def record_unloadable_brief(error: str, base=None) -> RunSummary:
    """A brief that cannot be loaded still leaves a decision log and a stop memo (need more info)."""
    base = Path(base) if base is not None else ROOT
    run_id = new_run_id()
    out_dir = base / "runs" / run_id
    logger = DecisionLogger(out_dir / "decision_log.json", run_id=run_id)
    summary = RunSummary(run_id=run_id, output_dir=out_dir, log_path=out_dir / "decision_log.json")
    ctx = RunContext(brief=None, run_id=run_id, logger=logger, approver=DenyAllApprover(), output_dir=out_dir)
    logger.log("orchestrator", "start_run", demo=False, steps=[], approver_type="DenyAllApprover")
    logger.log("orchestrator", "need_more_info", outcome="stop", reason=str(error)[:300])
    summary.stop_reasons = ["The brief could not be loaded, so nothing ran. Please describe the idea "
                            "(fill in at least the 'idea' field).", str(error)]
    memo = _write_stop_memo(ctx, summary, 0, "before any step ran, at the brief check", "STOP",
                            summary.stop_reasons, ["A brief file with an 'idea' that says what the product is "
                                                   "and who it is for."])
    logger.log("orchestrator", "write_stop_memo", outcome="stop", phase=0, output_file=memo)
    summary.status, summary.final_outcome = "stopped", "stop"
    logger.log("orchestrator", "end_run", cost=0, status="stopped", outcome="stop", tokens=0)
    return summary


def _non_negative_int(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def _phase_list(text):
    try:
        phases = sorted({int(p) for p in str(text).split(",") if p.strip()})
    except ValueError:
        raise argparse.ArgumentTypeError("use a comma-separated list such as 1,2") from None
    if not phases or any(p < 1 for p in phases):
        raise argparse.ArgumentTypeError("use a comma-separated list such as 1,2")
    return phases


def main(argv=None, **engine) -> int:
    """CLI entry point. ``engine`` passes injected dependencies (e.g. llm_client) to run_pipeline;
    it is for code that calls ``main`` directly (tests), never a setting."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Run the venture pipeline for one founder brief.")
    parser.add_argument("--brief", default=None,
                        help="path to a brief (.json), or a text file with free text (optional with --resume)")
    parser.add_argument("--metric", type=float, default=None,
                        help="MOCKED validation metric for the STUBBED validate step (e.g. 12)")
    parser.add_argument("--fast", action="store_true",
                        help="smaller Phase 1 research budget (2 discovery queries, 5 page fetches)")
    parser.add_argument("--interviews", default=None,
                        help="a text/markdown file with the founder's REAL interview notes to summarise")
    parser.add_argument("--demo", action="store_true",
                        help="write to outputs/ and the root decision_log.json instead of runs/<run_id>/")
    parser.add_argument("--phases", type=_phase_list, default=None, help="phases to run, e.g. 1,2 (default: all)")
    parser.add_argument("--resume", default=None, metavar="RUN_ID", help="continue a saved run (runs/<run_id>/)")
    parser.add_argument("--from", dest="from_phase", default="phase2", help="with --resume: phase2 (default)")
    parser.add_argument("--waitlist", default=None, help="Phase 2: a waitlist export (CSV); only counts are kept")
    parser.add_argument("--signups", type=_non_negative_int, default=None, help="Phase 2: founder-reported sign-ups")
    parser.add_argument("--visitors", type=_non_negative_int, default=None, help="Phase 2: founder-reported visitors")
    parser.add_argument("--mock-signups", type=_non_negative_int, default=None,
                        help="Phase 2: MOCKED sign-ups (demonstration only; never a real validation)")
    parser.add_argument("--mock-visitors", type=_non_negative_int, default=None,
                        help="Phase 2: MOCKED visitors (demonstration only)")
    args = parser.parse_args(argv)

    if args.resume and args.demo:
        print("--resume works on runs under runs/<run_id>/; it cannot be combined with --demo.", file=sys.stderr)
        return 2
    if args.resume and str(args.from_phase).lower().replace("phase", "") != "2":
        print("--from supports phase2 only.", file=sys.stderr)
        return 2
    if not args.resume and not args.brief:
        print("Give --brief (or --resume <run_id> to continue a saved run).", file=sys.stderr)
        return 2
    if not args.resume and args.phases and 1 not in args.phases:
        print("Phase 2 needs Phase 1 results: run --phases 1,2, or use --resume <run_id> --from phase2.",
              file=sys.stderr)
        return 2

    brief = None
    if args.brief:
        path = Path(args.brief)
        try:
            if path.suffix.lower() == ".json":
                brief = load_brief(path)
            else:
                text = path.read_text(encoding="utf-8-sig")
                # An empty or near-empty text: no model call; the run asks for more information.
                brief = FounderBrief(idea=text.strip()) if _idea_is_empty(text) else parse_brief_text(text)
        except (BriefError, OSError, UnicodeDecodeError) as e:
            print(f"Could not load the brief: {e}", file=sys.stderr)
            try:
                memo_run = record_unloadable_brief(str(e))
                print(f"Logged as run {memo_run.run_id} (stop memo and decision log in {memo_run.output_dir}).",
                      file=sys.stderr)
            except Exception as log_error:  # never crash in front of the founder
                print(f"(the stop could not be logged: {log_error})", file=sys.stderr)
            return 2

    if brief is not None:
        print(f"Brief: {brief.idea}")
    try:
        summary = run_pipeline(brief, approver=CLIApprover(), demo=args.demo, metric=args.metric, echo=print,
                               fast=args.fast, interviews_path=args.interviews, phases=args.phases,
                               resume_run_id=args.resume, from_phase=args.from_phase if args.resume else None,
                               waitlist_path=args.waitlist, signups=args.signups, visitors=args.visitors,
                               mock_signups=args.mock_signups, mock_visitors=args.mock_visitors, **engine)
    except ResumeError as e:
        print(f"Could not resume: {e}", file=sys.stderr)
        return 2
    print(format_summary(summary))
    flags = " ".join(x for x in (f"--phases {','.join(map(str, args.phases))}" if args.phases else "",
                                 "--fast" if args.fast else "", f"--resume {args.resume}" if args.resume else "") if x)
    print(f"Total run time: {summary.seconds:.1f}s" + (f" ({flags})" if flags else ""))
    return 1 if summary.status == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
