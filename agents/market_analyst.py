"""Step 1.3 market (market_analyst): segment, beachhead, use case, sourced competitors, market size."""
from __future__ import annotations

from agents.common import (brief_text, current_idea, fill, md_escape, prompt_sections, resolve_source,
                           safe_text, system_prompt)
from steps import Claim, StepResult, register_step
from tools.research import domain_of

ROLE = "market_analyst"
SCHEMA = {
    "type": "object",
    "properties": {
        "segment": {"type": "string"},
        "beachhead": {"type": "string"},
        "use_case": {"type": "string"},
        "competitors": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "source": {"type": "string"}, "note": {"type": "string"}},
            "required": ["name", "source", "note"]}},
        "market_size": {"type": "object",
                        "properties": {"estimate": {"type": "string"}, "source": {"type": "string"},
                                       "reasoning": {"type": "string"}},
                        "required": ["estimate", "source", "reasoning"]},
    },
    "required": ["segment", "beachhead", "use_case", "competitors", "market_size"],
}


@register_step
class MarketStep:
    phase, step, name, agent = 1, 3, "market", ROLE

    def run(self, ctx) -> StepResult:
        sections = prompt_sections(ROLE)
        research = ctx.research
        plan_cfg = ctx.options.get("research_plan") or {"market_queries": 2, "results_per_query": 8}
        opp = ctx.latest(1, 1)
        d = (opp.data if opp else {}) or {}
        problem = d.get("problem_statement") or current_idea(ctx)
        target_user = d.get("target_user") or ctx.brief.target_market
        queries = [f"{target_user} software tool", f"number of {target_user} market size",
                   f"{problem[:60]} alternatives"][:plan_cfg["market_queries"]]
        rows = research.search_many(queries, ROLE, k=plan_cfg["results_per_query"]) if research else []
        urls = list(dict.fromkeys(r["url"] for r in rows))
        entries = [research.registry.get(u) for u in urls][:10] if research else []
        if research and len(entries) < 10:   # top up with what step 1.1 already retrieved
            entries += [e for e in research.registry.entries() if e["url"] not in urls][:10 - len(entries)]

        ctx.progress(f"    analysing {len(entries)} sources...")
        prompt = fill(sections["analyse"], brief=brief_text(ctx.brief, current_idea(ctx)), problem=problem,
                      target_user=target_user,
                      sources=research.source_block(ROLE, entries, 450) if entries else "(no source retrieved)")
        reply = ctx.llm_json(prompt, SCHEMA, ROLE, system_prompt(sections), "market_analyst.analyse")
        data = reply["data"]

        competitors = []
        for c in data["competitors"][:5]:
            name = safe_text(ctx, ROLE, c.get("name"), "competitor")
            if not name:
                continue
            url = resolve_source(ctx, ROLE, c.get("source"), name)
            if url is None:   # never invent a competitor: one without a retrieved source is dropped
                ctx.logger.log(ROLE, "drop_unsourced_competitor", competitor=name[:200])
                continue
            competitors.append({"name": name, "url": url, "note": safe_text(ctx, ROLE, c.get("note"), "note") or ""})

        size = data["market_size"]
        size_url = resolve_source(ctx, ROLE, size.get("source"), size.get("estimate")) \
            if size.get("source", "").strip() else None
        estimate = (safe_text(ctx, ROLE, size.get("estimate"), "market_size") or "unknown").strip()
        reasoning = safe_text(ctx, ROLE, size.get("reasoning"), "market_size") or "no reasoning given"
        if size_url and estimate.lower() != "unknown":
            size_text = f"{estimate} (as stated by the source)"
        else:
            size_url = None
            size_text = f"unknown — ASSUMPTION: {reasoning}"

        segment = safe_text(ctx, ROLE, data["segment"], "segment") or "unknown"
        beachhead = safe_text(ctx, ROLE, data["beachhead"], "beachhead") or "unknown"
        use_case = safe_text(ctx, ROLE, data["use_case"], "use_case") or "unknown"
        claims = [Claim(f"Segment: {segment}", None), Claim(f"Beachhead customer: {beachhead}", None),
                  Claim(f"Main use case: {use_case}", None)]
        claims += [Claim(f"Competitor or alternative: {c['name']} ({c['note']})", c["url"]) for c in competitors]
        claims.append(Claim(f"Market size: {size_text}", size_url))
        lines = ["# Phase 1, step 3: market", "",
                 f"- Segment (ASSUMPTION): {segment}", f"- Beachhead customer (ASSUMPTION): {beachhead}",
                 f"- Main use case (ASSUMPTION): {use_case}", "", "## Competitors and alternatives", ""]
        if competitors:
            lines += ["| Name | Note | Source |", "|---|---|---|"]
            lines += [f"| {md_escape(c['name'])} | {md_escape(c['note'])} | [{domain_of(c['url'])}]({c['url']}) |"
                      for c in competitors]
        else:
            lines.append("No competitor could be tied to a retrieved source, so none is listed.")
        lines += ["", "## Market size", "",
                  f"- {size_text}" + (f" [source]({size_url})" if size_url else "")]
        out = {"segment": segment, "beachhead": beachhead, "use_case": use_case, "competitors": competitors,
               "market_size": {"text": size_text, "url": size_url}, "queries": queries}
        return StepResult(self.phase, self.step, self.name, self.agent, "\n".join(lines), claims, out,
                          reply.get("model"), 0, reply.get("tokens"), "WORKING")
