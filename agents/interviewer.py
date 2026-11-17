"""Step 1.4 interviews (interviewer): Mom Test script, recruiting plan, outreach DRAFT,
and an optional summary of the founder's REAL interview notes (``--interviews notes.md``).

Nothing is sent from here. ``send_outreach`` is the only way to send a draft: it goes
through ``approval.guarded_action("send", ...)`` and its executor is MOCKED.
"""
from __future__ import annotations

import re
from pathlib import Path

from agents.common import brief_text, current_idea, fill, md_escape, prompt_sections, safe_text, system_prompt
from approval import guarded_action, mock_send
from steps import Claim, StepResult, register_step
from tools.research import neutralise, wrap_untrusted

ROLE = "interviewer"
NOTES_SOURCE = "founder interview notes"
_BAD_QUESTION = re.compile(
    r"\b(?:would you|will you|could you see yourself|do you think|wouldn't it|isn't it|don't you|"
    r"if (?:we|i|there) (?:built|made|offered|had|were)|how much would|would it|imagine|our (?:product|app|tool))\b",
    re.I)
FALLBACK_QUESTIONS = [
    "Tell me about the last time you ran into this problem. What happened?",
    "How do you handle this today, step by step?",
    "What have you already tried to fix it? What did you like or dislike about it?",
    "What did that cost you, in money or in time?",
    "How often has this come up in the last month?",
    "Who else is involved when this happens?",
    "When did you last look for a better way? What did you find?",
    "What happened the last time it went wrong?",
    "Is there anyone else you think I should talk to about this?",
]
SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "recruit": {"type": "object",
                    "properties": {"who": {"type": "string"},
                                   "where": {"type": "array", "items": {"type": "string"}},
                                   "how": {"type": "string"}},
                    "required": ["who", "where", "how"]},
        "outreach_draft": {"type": "string"},
    },
    "required": ["questions", "recruit", "outreach_draft"],
}
NOTES_SCHEMA = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {
        "type": "object",
        "properties": {"text": {"type": "string"},
                       "type": {"type": "string", "enum": ["pain", "current_workaround", "willingness_to_pay",
                                                           "objection", "other"]}},
        "required": ["text", "type"]}}},
    "required": ["findings"],
}


def mom_test_questions(raw) -> list:
    """Keep past-behaviour questions; drop leading or hypothetical ones; 8-10 in total."""
    kept = []
    for q in raw:
        q = re.sub(r"\s+", " ", str(q or "")).strip()
        if q and not _BAD_QUESTION.search(q) and q not in kept:
            kept.append(q)
    for q in FALLBACK_QUESTIONS:
        if len(kept) >= 8:
            break
        if q not in kept:
            kept.append(q)
    return kept[:10]


def send_outreach(ctx, draft: str, recipients_note: str = "recipients chosen by the founder") -> object:
    """Send one outreach draft ONLY with founder approval. The executor is MOCKED: nothing is sent."""
    return guarded_action("send", "Send an interview outreach message (MOCKED executor: nothing is sent)",
                          {"draft": draft, "recipients": recipients_note}, mock_send, ctx)


@register_step
class InterviewsStep:
    phase, step, name, agent = 1, 4, "interviews", ROLE
    repeat_from = (1, 1)   # a Phase 1 REPEAT reruns discovery (1.1) with new queries
    pivot_to = (1, 1)      # a Phase 1 PIVOT reruns Phase 1 on the chosen alternative

    def default_gates(self) -> list:
        from gates import Phase1EvidenceGate
        return [Phase1EvidenceGate()]

    def run(self, ctx) -> StepResult:
        sections = prompt_sections(ROLE)
        system = system_prompt(sections)
        opp = ctx.latest(1, 1)
        d = (opp.data if opp else {}) or {}
        problem = d.get("problem_statement") or current_idea(ctx)
        target_user = d.get("target_user") or ctx.brief.target_market
        brief = brief_text(ctx.brief, current_idea(ctx))
        reply = ctx.llm_json(fill(sections["script"], brief=brief, problem=problem, target_user=target_user),
                             SCRIPT_SCHEMA, ROLE, system, "interviewer.script")
        tokens, model = reply.get("tokens") or 0, reply.get("model")
        data = reply["data"]
        questions = [q for q in (safe_text(ctx, ROLE, q, "question") for q in mom_test_questions(data["questions"]))
                     if q]
        recruit = data["recruit"]
        who = safe_text(ctx, ROLE, recruit.get("who"), "recruit") or target_user
        where = [w for w in (safe_text(ctx, ROLE, w, "recruit") for w in recruit.get("where", [])[:4]) if w]
        how = safe_text(ctx, ROLE, recruit.get("how"), "recruit") or ""
        draft = safe_text(ctx, ROLE, data["outreach_draft"], "outreach_draft")

        findings, claims = [], []
        notes_path = ctx.options.get("interviews_path")
        if notes_path:
            findings, t = self._summarise_notes(ctx, sections, system, brief, Path(notes_path))
            tokens += t
            claims += [Claim(f"Interview finding ({f['type']}): {f['text']}", None, NOTES_SOURCE) for f in findings]

        lines = ["# Phase 1, step 4: interviews", "",
                 "A customer-interview kit in the style of The Mom Test: past behaviour only, no leading "
                 "or hypothetical questions, no pitching.", "", "## Interview script", ""]
        lines += [f"{i}. {q}" for i, q in enumerate(questions, 1)]
        lines += ["", "## Who to recruit and where", "", f"- Who: {who}"]
        lines += [f"- Where: {w}" for w in where]
        lines += [f"- How (no spam): {how}" if how else
                  "- How (no spam): ask in places they already talk, one person at a time, and say who you are."]
        lines += ["", "## Outreach message (DRAFT)", "",
                  "DRAFT only. Nothing has been sent. Sending needs the founder's explicit approval, and the "
                  "send executor is MOCKED.", ""]
        lines += [f"> {line}" for line in (draft or "(no draft passed the output check)").splitlines()]
        lines += ["", "## Interview evidence", ""]
        if findings:
            lines.append(f"Summarised from the founder's real interview notes ({md_escape(notes_path)}). "
                         "These are not web sources, so the Phase 1 gate does not count them.")
            lines += [f"- {f['type']}: {md_escape(f['text'])}" for f in findings]
        else:
            lines.append("No real interviews yet. Real interviews are needed before the Phase 2 gate.")
        out = {"questions": questions, "recruit": {"who": who, "where": where, "how": how},
               "outreach_draft": draft, "interview_findings": findings, "outreach_sent": False}
        return StepResult(self.phase, self.step, self.name, self.agent, "\n".join(lines), claims, out,
                          model, 0, tokens, "WORKING")

    def _summarise_notes(self, ctx, sections, system, brief, path: Path):
        try:
            raw = path.read_text(encoding="utf-8-sig")[:6000]
        except OSError as e:
            ctx.logger.log(ROLE, "interview_notes_unreadable", error=str(e))
            return [], 0
        clean, removed = neutralise(raw)
        if removed:
            ctx.logger.log(ROLE, "untrusted_instruction_removed", url=None, file=str(path),
                           removed=[r[:200] for r in removed])
        reply = ctx.llm_json(fill(sections["notes"], brief=brief, notes=wrap_untrusted("NOTES", "file", clean)),
                             NOTES_SCHEMA, ROLE, system, "interviewer.notes")
        findings = []
        for f in reply["data"]["findings"][:8]:
            text = safe_text(ctx, ROLE, f.get("text"), "interview_finding")
            if text:
                findings.append({"text": text, "type": f.get("type"), "source": NOTES_SOURCE})
        ctx.logger.log(ROLE, "summarise_interview_notes", findings=len(findings), source=NOTES_SOURCE)
        return findings, reply.get("tokens") or 0


@register_step
class OutreachStep:
    """Runs only after the Phase 1 gate said GO. Asks the founder whether to send the outreach
    draft. The send executor is MOCKED: even when approved, nothing leaves this machine."""
    phase, step, name, agent = 1, 5, "outreach", ROLE

    def run(self, ctx) -> StepResult:
        kit = ctx.latest(1, 4)
        draft = ((kit.data if kit else {}) or {}).get("outreach_draft")
        if not draft:
            content = ("# Phase 1, step 5: outreach (MOCKED send)\n\n"
                       "No outreach draft passed the output check, so nothing was offered for sending.")
            return StepResult(self.phase, self.step, self.name, self.agent, content, [],
                              {"sent": False, "decision": None}, None, 0, None, "MOCKED")
        action = send_outreach(ctx, draft)
        if action.approved and action.executed:
            outcome = ("The founder approved sending. The send executor is MOCKED: no message was "
                       "actually sent to anyone.")
        elif action.decision == "rejected":
            outcome = "The founder said No. Nothing was sent; the draft stays a draft."
        else:
            outcome = "No founder approval was recorded, so it was denied by default. Nothing was sent."
        content = ("# Phase 1, step 5: outreach (MOCKED send)\n\n"
                   f"- Decision: {action.decision}\n- Result: {outcome}\n\n"
                   "Real interviews (by the founder, under their own name) are needed before the Phase 2 gate.")
        return StepResult(self.phase, self.step, self.name, self.agent, content, [],
                          {"sent_mocked": bool(action.executed), "decision": action.decision},
                          None, 0, None, "MOCKED")
