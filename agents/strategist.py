"""Step 2.1 lean_canvas (strategist): the 9-box Lean Canvas from the Phase 1 results.

Every item is a Claim: it reuses a source retrieved in Phase 1, or it is ASSUMPTION (the
SourceRegistry downgrade applies). Revenue is a price HYPOTHESIS (ASSUMPTION), never a
projection. The founder approves the canvas; a rejection redoes the step with the note.
"""
from __future__ import annotations

import re

from agents.common import brief_text, current_idea, fill, md_escape, prompt_sections, resolve_source, safe_text, \
    system_prompt
from agents.phase2_common import mocked_banner, phase1_findings
from steps import Claim, StepResult, register_step

ROLE = "strategist"
BOXES = ["problem", "customer_segments", "unique_value_proposition", "solution", "channels", "revenue_streams",
         "cost_structure", "key_metrics", "unfair_advantage"]
TITLES = {"problem": "Problem", "customer_segments": "Customer segments",
          "unique_value_proposition": "Unique value proposition", "solution": "Solution", "channels": "Channels",
          "revenue_streams": "Revenue streams", "cost_structure": "Cost structure", "key_metrics": "Key metrics",
          "unfair_advantage": "Unfair advantage"}
ALWAYS_ASSUMPTION = {"revenue_streams", "unfair_advantage", "solution", "unique_value_proposition"}
# Projections, forecasts and income promises are dropped from the canvas (and logged).
_PROJECTION = re.compile(
    r"\b(?:projected|projection|forecast|expected revenue|will (?:earn|make|generate)|guarantee\w*|"
    r"mrr|arr|run[- ]rate|passive income|profit of|revenue of|\d[\d,.]*\s*(?:k\s*)?(?:customers|users|sales|"
    r"subscribers|buyers)\b|(?:per|a|each)\s+(?:month|year)\s+in\s+(?:revenue|sales|income))", re.I)

_ITEM = {"type": "object", "properties": {"text": {"type": "string"}, "source": {"type": "string"}},
         "required": ["text", "source"]}
SCHEMA = {
    "type": "object",
    "properties": {
        **{box: {"type": "array", "items": _ITEM, "minItems": 1} for box in BOXES},
        "price_hypothesis": {"type": "string"},
        "riskiest_assumptions": {"type": "array", "minItems": 1, "items": {
            "type": "object", "properties": {"assumption": {"type": "string"}, "cheap_test": {"type": "string"}},
            "required": ["assumption", "cheap_test"]}},
    },
    "required": BOXES + ["price_hypothesis", "riskiest_assumptions"],
}
FALLBACK_RISKS = [
    ("The target users have this problem often enough to want a fix.",
     "Ask 5 people from the target group about the last time it happened (Mom Test, no pitch)."),
    ("They would join a waitlist for a digital product that solves it.",
     "Run the Phase 2 waitlist test against the pre-registered target."),
    ("They would pay the hypothesised price.",
     "After sign-up, ask what they pay today for workarounds; no sale is taken."),
]


def _clean(ctx, text, where):
    text = safe_text(ctx, ROLE, text, where)
    if text and _PROJECTION.search(text):
        ctx.logger.log(ROLE, "drop_projection", where=where, text=text[:200],
                       reason="projections, forecasts and income promises are not allowed in the canvas")
        return None
    return text


def canvas_approval_payload(result, ctx) -> dict:
    data = result.data or {}
    return {"what": "Lean Canvas (step 2.1)", "unfair_advantage": data.get("boxes", {}).get("unfair_advantage"),
            "price_hypothesis": data.get("price_hypothesis"),
            "riskiest_assumptions": [r["assumption"] for r in data.get("riskiest_assumptions", [])]}


def record_signoff(payload) -> dict:
    """Records the founder's sign-off. Nothing leaves this machine."""
    return {"status": "WORKING", "kind": "sign", "detail": f"founder signed off: {payload.get('what')}"}


@register_step
class LeanCanvasStep:
    phase, step, name, agent = 2, 1, "lean_canvas", ROLE

    def default_gates(self) -> list:
        from gates import FounderApprovalGate, Outcome
        return [FounderApprovalGate(
            "sign", "Sign off the Lean Canvas (step 2.1) before the demand test is designed. "
            "Answer No to have it redone.", record_signoff, canvas_approval_payload,
            reject_outcome=Outcome.REPEAT, name="founder_approval_canvas")]

    def run(self, ctx) -> StepResult:
        sections = prompt_sections(ROLE)
        findings, urls = phase1_findings(ctx)
        note = ctx.options.pop("founder_note_2_1", None)
        note_text = f"\nThe founder rejected the previous canvas. Their note: {note}\n" if note else ""
        if note:
            ctx.logger.log(ROLE, "use_founder_note", note=str(note)[:300])
        reply = ctx.llm_json(fill(sections["canvas"], brief=brief_text(ctx.brief, current_idea(ctx)),
                                  findings=findings, note=note_text),
                             SCHEMA, ROLE, system_prompt(sections), "strategist.canvas")
        data = reply["data"]
        boxes, claims = {}, []
        for box in BOXES:
            items = []
            for item in data.get(box, [])[:3]:
                text = _clean(ctx, item.get("text"), box)
                if not text:
                    continue
                url = None
                if box not in ALWAYS_ASSUMPTION and str(item.get("source") or "").strip():
                    url = resolve_source(ctx, ROLE, item.get("source"), text)
                    if url and url not in urls:   # only sources Phase 1 actually used count
                        ctx.logger.log(ROLE, "downgrade_to_assumption", cited=url, claim=text[:300],
                                       reason="the source was retrieved but is not a Phase 1 finding")
                        url = None
                items.append({"text": text, "url": url})
            if box == "unfair_advantage" and not items:
                items = [{"text": "None yet", "url": None}]
            if not items:
                items = [{"text": "unknown (no usable item)", "url": None}]
            boxes[box] = items
            claims += [Claim(f"{TITLES[box]}: {i['text']}", i["url"]) for i in items]
        price = _clean(ctx, data.get("price_hypothesis"), "price_hypothesis") or "not set"
        price = f"Price hypothesis (ASSUMPTION, to be tested; not a forecast): {price}"
        claims.append(Claim(price, None))
        risks = []
        for r in data.get("riskiest_assumptions", [])[:3]:
            a, t = _clean(ctx, r.get("assumption"), "risk"), _clean(ctx, r.get("cheap_test"), "risk")
            if a and t:
                risks.append({"assumption": a, "cheap_test": t})
        for a, t in FALLBACK_RISKS:
            if len(risks) >= 3:
                break
            risks.append({"assumption": a, "cheap_test": t})
        content = self._markdown(ctx, boxes, price, risks)
        out = {"boxes": {b: [i["text"] for i in boxes[b]] for b in BOXES}, "box_sources": {
            b: [i["url"] for i in boxes[b]] for b in BOXES}, "price_hypothesis": price, "riskiest_assumptions": risks}
        return StepResult(self.phase, self.step, self.name, self.agent, content, claims, out,
                          reply.get("model"), 0, reply.get("tokens"), "WORKING")

    @staticmethod
    def _markdown(ctx, boxes, price, risks) -> str:
        def cell(box):
            return "<br>".join(md_escape(i["text"]) + (f" [source]({i['url']})" if i["url"] else " (ASSUMPTION)")
                               for i in boxes[box])

        lines = ["# Phase 2, step 1: Lean Canvas", "", *mocked_banner(ctx),
                 "Built from the Phase 1 results. An item with a source link reuses a page retrieved in "
                 "Phase 1; everything else is labelled ASSUMPTION. No revenue projections and no income promises.",
                 "", "| Box | Items |", "|---|---|"]
        lines += [f"| {TITLES[b]} | {cell(b)} |" for b in BOXES]
        lines += ["", f"- {price}", "", "## Riskiest assumptions (most risky first)", ""]
        lines += [f"{n}. {md_escape(r['assumption'])} (ASSUMPTION). Cheap test: {md_escape(r['cheap_test'])}"
                  for n, r in enumerate(risks, 1)]
        return "\n".join(lines)
