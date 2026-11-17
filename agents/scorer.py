"""Step 1.2 idea_scoring (scorer): 1-5 sub-scores from the model, weighted total computed in Python."""
from __future__ import annotations

import json
from pathlib import Path

from agents.common import brief_text, current_idea, fill, md_escape, prompt_sections, sourced_signals, system_prompt
from steps import Claim, StepResult, register_step

ROLE = "scorer"
ROOT = Path(__file__).resolve().parent.parent
LLM_CRITERIA = ["founder_fit", "budget_feasibility", "time_feasibility", "values_alignment",
                "differentiation", "digital_delivery"]
CRITERIA = ["pain_evidence"] + LLM_CRITERIA


def load_scoring() -> dict:
    return json.loads((ROOT / "config" / "scoring.json").read_text(encoding="utf-8"))


def pain_score(n_sourced: int, bands) -> int:
    """Pain evidence (1-5) from the number of sourced signals only."""
    score = 1
    for minimum, value in bands:
        if n_sourced >= minimum:
            score = value
    return int(score)


def weighted_total(scores: dict, weights: dict) -> float:
    """sum(weight * score) / sum(weights), on the 1-5 scale, rounded to 2 decimals."""
    total_w = sum(weights[c] for c in CRITERIA)
    return round(sum(weights[c] * scores[c] for c in CRITERIA) / total_w, 2)


def _schema(ids):
    item = {"type": "object", "properties": {"id": {"type": "string", "enum": ids}}, "required": ["id"]}
    for c in LLM_CRITERIA:
        item["properties"][c] = {"type": "integer", "minimum": 1, "maximum": 5}
        item["properties"][c + "_why"] = {"type": "string"}
        item["required"] += [c, c + "_why"]
    return {"type": "object",
            "properties": {"ideas": {"type": "array", "items": item, "minItems": 1},
                           "hard_blocker": {"type": "string"}},
            "required": ["ideas", "hard_blocker"]}


@register_step
class IdeaScoringStep:
    phase, step, name, agent = 1, 2, "idea_scoring", ROLE

    def run(self, ctx) -> StepResult:
        config = load_scoring()
        weights, bands = config["weights"], config["pain_evidence_bands"]
        opp = ctx.latest(1, 1)
        opp_data = (opp.data if opp else {}) or {}
        signals = opp_data.get("pain_signals") or []
        sourced = sourced_signals(ctx)
        sourced_keys = {(s["url"], s["text"]) for s in sourced}
        ideas = [{"id": "main", "name": current_idea(ctx), "signals": len(sourced)}]
        for i, alt in enumerate(opp_data.get("alternatives") or [], start=1):
            own = [signals[j] for j in alt.get("signal_indexes", []) if j < len(signals)]
            own_sourced = [s for s in own if (s.get("url"), s.get("text")) in sourced_keys]
            ideas.append({"id": f"alt{i}", "name": f"{alt['name']}: {alt['description']}",
                          "short": alt["name"], "signals": len(own_sourced)})

        sections = prompt_sections(ROLE)
        listing = "\n".join(f"{x['id']}: {x['name']}" for x in ideas)
        evidence = (f"{len(sourced)} sourced pain signals for the main idea; "
                    + ", ".join(f"{x['id']} has {x['signals']}" for x in ideas[1:]))
        reply = ctx.llm_json(fill(sections["score"], brief=brief_text(ctx.brief, current_idea(ctx)),
                                  ideas=listing, evidence=evidence),
                             _schema([x["id"] for x in ideas]), ROLE, system_prompt(sections), "scorer.score")
        by_id = {}
        for row in reply["data"]["ideas"]:
            by_id.setdefault(row["id"], row)

        for idea in ideas:
            row = by_id.get(idea["id"])
            scores, why = {}, {}
            n = idea["signals"]
            scores["pain_evidence"] = pain_score(n, bands)
            why["pain_evidence"] = f"computed in Python from {n} sourced pain signal(s)"
            for c in LLM_CRITERIA:
                if row is None:
                    scores[c], why[c] = 1, "no score returned by the model (ASSUMPTION: lowest score)"
                else:
                    scores[c] = max(1, min(5, int(row[c])))
                    why[c] = (row.get(c + "_why") or "").strip() or "no justification given"
            idea["scores"], idea["why"] = scores, why
            idea["total"] = weighted_total(scores, weights)

        main = ideas[0]
        ranked = sorted(ideas, key=lambda x: (-x["total"], x["id"] != "main"))
        better = [x for x in ideas[1:] if x["total"] > main["total"]]
        blocker = (reply["data"].get("hard_blocker") or "").strip()
        content = self._markdown(ranked, main, better, blocker, weights)
        claims = [Claim(f"Main idea weighted score: {main['total']:.2f}/5 (model sub-scores, weights from "
                        f"config/scoring.json; pain evidence from sourced signals only)", None)]
        data = {"ideas": ideas, "ranking": [{"id": x["id"], "total": x["total"], "signals": x["signals"]}
                                             for x in ranked],
                "main_total": main["total"], "hard_blocker": blocker, "weights": weights,
                "alternative_beats_main": bool(better)}
        return StepResult(self.phase, self.step, self.name, self.agent, content, claims, data,
                          reply.get("model"), 0, reply.get("tokens"), "WORKING")

    @staticmethod
    def _markdown(ranked, main, better, blocker, weights):
        head = "| Rank | Idea | Total (1-5) | " + " | ".join(CRITERIA) + " |"
        lines = ["# Phase 1, step 2: idea scoring", "",
                 "Sub-scores 1-5. Pain evidence is computed from sourced pain signals only; the other "
                 "sub-scores are the model's judgement (ASSUMPTION). The weighted total is computed in "
                 "Python with the weights in `config/scoring.json`: "
                 + ", ".join(f"{k} {v}" for k, v in weights.items()) + ".", "",
                 head, "|" + "---|" * (len(CRITERIA) + 3)]
        for rank, x in enumerate(ranked, 1):
            label = "MAIN: " + x["name"] if x["id"] == "main" else x["name"]
            lines.append(f"| {rank} | {md_escape(label)[:120]} | **{x['total']:.2f}** | "
                         + " | ".join(str(x["scores"][c]) for c in CRITERIA) + " |")
        lines += ["", "## Why (main idea)", ""]
        lines += [f"- {c}: {main['scores'][c]} ({md_escape(main['why'][c])})" for c in CRITERIA]
        lines.append("")
        if better:
            top = max(better, key=lambda x: x["total"])
            lines.append(f"**An alternative beats the original idea:** {md_escape(top.get('short', top['name']))} "
                         f"scores {top['total']:.2f} against {main['total']:.2f}.")
        else:
            lines.append("No alternative scores higher than the original idea.")
        if blocker:
            lines += ["", f"**Hard founder-fit blocker named by the model:** {md_escape(blocker)}"]
        return "\n".join(lines)
