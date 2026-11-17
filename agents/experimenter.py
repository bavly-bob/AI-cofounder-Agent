"""Step 2.2 experiment (experimenter): one demand test with a target the founder approves
BEFORE any result exists.

The approved target and its SHA-256 hash are logged. After approval the target cannot be
changed (``change_target`` refuses and logs); only a new experiment (a REPEAT of this step,
approved again) replaces it. The agent may only propose a STRICTER target than the default.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from agents.common import brief_text, current_idea, fill, md_escape, prompt_sections, safe_text, system_prompt
from agents.phase2_common import load_phase2, mocked_banner
from steps import Claim, StepResult, register_step

ROLE = "experimenter"
SCHEMA = {
    "type": "object",
    "properties": {
        "hypothesis": {"type": "string"},
        "channels": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "kill_criteria": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "message": {"type": "string"},
        "suggested_target": {"type": "integer", "minimum": 0},
        "target_reason": {"type": "string"},
    },
    "required": ["hypothesis", "channels", "kill_criteria", "message", "suggested_target", "target_reason"],
}


def target_hash(target: dict) -> str:
    """SHA-256 of the target, in a fixed (sorted-key) JSON form."""
    return hashlib.sha256(json.dumps(target, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def propose_target(config: dict, suggested, reason: str) -> tuple:
    """(target, note). The default from config, or the agent's suggestion if it is STRICTER and has a reason."""
    target = {"metric": "waitlist_signups", "signups": int(config["target_signups"]),
              "window_days": int(config["window_days"]), "min_conversion": float(config["min_conversion"])}
    note = "default target from config/thresholds.json"
    if isinstance(suggested, int) and suggested and suggested != target["signups"]:
        if target["signups"] < suggested <= int(config["max_target_signups"]) and reason:
            target["signups"] = suggested
            note = f"stricter target suggested by the agent: {reason}"
        else:
            note = (f"the agent suggested {suggested} sign-ups; not adopted (only a stricter target, with a "
                    f"reason, up to {config['max_target_signups']}, may replace the default)")
    return target, note


def register_target(ctx, target: dict) -> dict:
    """Called only after the founder approved the target. Stores it with its hash and logs it."""
    old = ctx.options.get("experiment_target")
    record = {"target": dict(target), "sha256": target_hash(target), "approved_at": _now(),
              "experiment_no": (old or {}).get("experiment_no", 0) + 1}
    if old:
        ctx.logger.log(ROLE, "new_experiment", approved_by="founder", previous_sha256=old.get("sha256"),
                       sha256=record["sha256"], target=record["target"], experiment_no=record["experiment_no"],
                       reason="a new experiment (REPEAT of step 2.2) replaces the earlier target")
    ctx.options["experiment_target"] = record
    ctx.logger.log(ROLE, "register_experiment_target", approved_by="founder", target=record["target"],
                   sha256=record["sha256"], approved_at=record["approved_at"], experiment_no=record["experiment_no"])
    return record


def change_target(ctx, new_target: dict, reason: str = "") -> bool:
    """Any change to an approved target is refused and logged. Returns True only before approval."""
    reg = ctx.options.get("experiment_target")
    if reg and reg.get("approved_at"):
        ctx.logger.log(ROLE, "refuse_target_change", outcome="refused", requested=new_target,
                       registered=reg.get("target"), sha256=reg.get("sha256"), reason=str(reason)[:300],
                       why="the target was approved before results existed; only a new experiment can change it")
        return False
    return True


def target_payload(result, ctx) -> dict:
    d = result.data or {}
    t = d.get("target") or {}
    return {"what": "pre-registered demand-test target (step 2.2)", "target": t, "sha256": target_hash(t),
            "hypothesis": d.get("hypothesis"), "results_exist": False}


def approve_target_executor(payload) -> dict:
    """Records the approval. Nothing leaves this machine."""
    return {"status": "WORKING", "kind": "sign", "detail": "target pre-registered", "sha256": payload.get("sha256")}


def _on_target_approved(action, result, ctx) -> list:
    record = register_target(ctx, (result.data or {}).get("target") or {})
    return [f"Target pre-registered at {record['approved_at']} (SHA-256 {record['sha256']}). It cannot be changed "
            "now; only a new experiment can replace it."]


@register_step
class ExperimentStep:
    phase, step, name, agent = 2, 2, "experiment", ROLE

    def default_gates(self) -> list:
        from gates import FounderApprovalGate, Outcome
        return [FounderApprovalGate(
            "sign", "Approve the demand-test target BEFORE any result exists (see details). It is then locked "
            "with a SHA-256 hash. Answer No to have a new experiment designed.", approve_target_executor,
            target_payload, reject_outcome=Outcome.REPEAT, on_approved=_on_target_approved,
            name="founder_approval_target")]

    def run(self, ctx) -> StepResult:
        config = load_phase2()
        sections = prompt_sections(ROLE)
        canvas = ctx.latest(2, 1)
        boxes = ((canvas.data if canvas else {}) or {}).get("boxes") or {}
        summary = "\n".join(f"{k}: {'; '.join(v)}" for k, v in boxes.items()) or "(no canvas)"
        change = ctx.options.pop("experiment_change_note", None)
        note = ctx.options.pop("founder_note_2_2", None)
        extra = "\n".join(x for x in (change, f"The founder rejected the previous design: {note}" if note else None)
                          if x)
        if ctx.options.get("experiment_target"):
            ctx.logger.log(ROLE, "design_new_experiment", previous_sha256=ctx.options["experiment_target"]["sha256"],
                           reason=extra or "repeat")
        reply = ctx.llm_json(fill(sections["design"], brief=brief_text(ctx.brief, current_idea(ctx)), canvas=summary,
                                  target=config["target_signups"], days=config["window_days"],
                                  conversion=f"{config['min_conversion']:.0%}", change=extra),
                             SCHEMA, ROLE, system_prompt(sections), "experimenter.design")
        d = reply["data"]
        reason = safe_text(ctx, ROLE, d.get("target_reason"), "target_reason") or ""
        target, target_note = propose_target(config, d.get("suggested_target"), reason)
        ctx.logger.log(ROLE, "propose_target", target=target, note=target_note, sha256=target_hash(target))
        hypothesis = safe_text(ctx, ROLE, d.get("hypothesis"), "hypothesis") or \
            f"At least {target['signups']} target users join the waitlist within {target['window_days']} days."
        channels = [c for c in (safe_text(ctx, ROLE, c, "channel") for c in d.get("channels", [])[:3]) if c]
        kill = [k for k in (safe_text(ctx, ROLE, k, "kill") for k in d.get("kill_criteria", [])[:3]) if k]
        message = safe_text(ctx, ROLE, d.get("message"), "message") or ""
        lines = ["# Phase 2, step 2: experiment", "", *mocked_banner(ctx),
                 "One demand test: a waitlist landing page. The founder approves the target below BEFORE any "
                 "result exists; it is then locked with a SHA-256 hash and cannot be changed (only a new "
                 "experiment can replace it).", "",
                 f"- Hypothesis (ASSUMPTION until tested): {md_escape(hypothesis)}",
                 "- Primary metric: waitlist sign-ups (deduplicated, valid emails; only counts are kept)",
                 "- Secondary metric: conversion = sign-ups / landing-page visitors",
                 f"- Target: >= {target['signups']} sign-ups in {target['window_days']} days, conversion >= "
                 f"{target['min_conversion']:.0%} ({md_escape(target_note)})",
                 f"- Target SHA-256 (before approval): `{target_hash(target)}`",
                 f"- Window: {target['window_days']} days from the founder's approval",
                 f"- Core message: {md_escape(message) or 'n/a'}", "", "## Channels (free; the founder posts as "
                 "themselves, after approval)", ""]
        lines += [f"- {md_escape(c)}" for c in channels] or ["- none proposed"]
        lines += ["", "## Kill criteria", ""] + ([f"- {md_escape(k)}" for k in kill] or ["- none proposed"])
        lines += ["- Below 50% of the target at the end of the window: STOP (or PIVOT to a well-scored Phase 1 "
                  "alternative)."]
        if change:
            lines += ["", f"This is a REPEAT: {md_escape(change)}"]
        claims = [Claim(f"Hypothesis: {hypothesis}", None)]
        data = {"hypothesis": hypothesis, "channels": channels, "kill_criteria": kill, "message": message,
                "target": target, "target_note": target_note, "proposed_sha256": target_hash(target)}
        return StepResult(self.phase, self.step, self.name, self.agent, "\n".join(lines), claims, data,
                          reply.get("model"), 0, reply.get("tokens"), "WORKING")
