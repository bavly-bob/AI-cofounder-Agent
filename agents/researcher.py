"""Step 1.1 opportunities (researcher): problem statement, sourced pain signals, alternatives."""
from __future__ import annotations

import json
from pathlib import Path

import guardrails
from agents.common import (brief_text, current_idea, domains, fill, md_escape, prompt_sections,
                           resolve_source, safe_text, system_prompt, word_overlap)
from steps import Claim, StepResult, register_step
from tools.research import domain_of

ROLE = "researcher"
ROOT = Path(__file__).resolve().parent.parent
SIGNAL_TYPES = ["complaint", "question", "paid_workaround"]
# Discussion sites first: they hold complaints and questions, which are what a pain signal is.
_DISCUSSION = ("reddit.com", "news.ycombinator.com", "stackexchange.com", "stackoverflow.com", "quora.com",
               "proz.com", "indiehackers.com", "community.", "forum", "discourse", "github.com")

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "problem_statement": {"type": "string"},
        "target_user": {"type": "string"},
        "queries": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "required": ["problem_statement", "target_user", "queries"],
}
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "pain_signals": {"type": "array", "items": {
            "type": "object",
            "properties": {"summary": {"type": "string"}, "source": {"type": "string"},
                           "type": {"type": "string", "enum": SIGNAL_TYPES}},
            "required": ["summary", "source", "type"]}},
        "alternatives": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "description": {"type": "string"},
                           "why_fits": {"type": "string"},
                           "signal_ids": {"type": "array", "items": {"type": "integer"}}},
            "required": ["name", "description", "why_fits", "signal_ids"]}},
    },
    "required": ["pain_signals", "alternatives"],
}


def min_overlap() -> float:
    """The honesty threshold for pain signals, from config/research.json (default 0.3)."""
    try:
        config = json.loads((ROOT / "config" / "research.json").read_text(encoding="utf-8"))
        return float(config.get("signal_min_word_overlap", 0.3))
    except (OSError, ValueError, TypeError):
        return 0.3


def verify_signal(ctx, text: str, url: str | None) -> tuple:
    """(url or None, overlap, "fetched page" | "search snippet"). A signal keeps its URL only if its wording overlaps the retrieved text."""
    if not url or ctx.research is None:
        return url, None, None
    entry = ctx.research.registry.get(url) or {}
    kind = "fetched page" if entry.get("text") else "search snippet"
    source_text = " ".join(str(entry.get(k) or "") for k in ("title", "snippet", "text"))
    overlap = round(word_overlap(text, source_text), 2)
    threshold = min_overlap()
    if overlap < threshold:
        ctx.logger.log(ROLE, "downgrade_unverified_signal", sources=[url], claim=text[:300], overlap=overlap,
                       threshold=threshold, compared_with=kind,
                       reason="the signal's wording does not match the retrieved source text")
        return None, overlap, kind
    return url, overlap, kind


def _discussion_rank(row) -> int:
    url = row.get("url", "")
    if row.get("provider") == "wikipedia":
        return 3
    return 0 if any(d in url for d in _DISCUSSION) else 1


@register_step
class OpportunitiesStep:
    phase, step, name, agent = 1, 1, "opportunities", ROLE

    def run(self, ctx) -> StepResult:
        sections = prompt_sections(ROLE)
        system = system_prompt(sections)
        research = ctx.research
        plan_cfg = ctx.options.get("research_plan") or {"discover_queries": 3, "results_per_query": 8,
                                                        "fetch_per_step": 8}
        idea = current_idea(ctx)
        brief = brief_text(ctx.brief, idea)
        previous = [r for r in ctx.results if (r.phase, r.step) == (1, 1)]
        old_queries = [q for r in previous for q in (r.data or {}).get("queries", [])]
        n_queries = plan_cfg["discover_queries"]
        avoid = ("Earlier queries (write DIFFERENT ones): " + "; ".join(old_queries)) if old_queries else ""
        tokens, model = 0, None

        ctx.progress("    planning queries...")
        plan = ctx.llm_json(fill(sections["plan"], brief=brief, avoid=avoid, n=n_queries), PLAN_SCHEMA,
                            ROLE, system, "researcher.plan")
        tokens, model = tokens + (plan.get("tokens") or 0), plan.get("model")
        problem = plan["data"]["problem_statement"].strip() or idea
        target_user = plan["data"]["target_user"].strip() or ctx.brief.target_market
        queries = [q for q in plan["data"]["queries"] if isinstance(q, str) and q.strip()]
        queries = [q for q in queries if q not in old_queries][:n_queries]

        rows = research.search_many(queries, ROLE, k=plan_cfg["results_per_query"]) if research else []
        ranked = sorted(rows, key=_discussion_rank)
        to_fetch = [r["url"] for r in ranked if r.get("provider") != "wikipedia"][:plan_cfg["fetch_per_step"]]
        if research:
            ctx.progress(f"    fetching {min(len(to_fetch), research.fetches_left())} pages...")
            research.fetch_many(to_fetch, ROLE)

        # Every source retrieved so far in the run (a repeat or pivot reuses earlier ones too).
        entries = sorted(research.registry.entries(), key=lambda e: (
            0 if "fetched" in e["kinds"] else 1, _discussion_rank({"url": e["url"], "provider": e["provider"]})
        ))[:12] if research else []
        signals, alternatives = [], []
        if entries:
            ctx.progress(f"    reading {len(entries)} sources...")
            reply = ctx.llm_json(fill(sections["extract"], brief=brief, problem=problem,
                                      sources=research.source_block(ROLE, entries)),
                                 EXTRACT_SCHEMA, ROLE, system, "researcher.extract")
            tokens += reply.get("tokens") or 0
            model = reply.get("model") or model
            signals, index_map = self._signals(ctx, reply["data"]["pain_signals"])
            alternatives = self._alternatives(ctx, reply["data"]["alternatives"], index_map)
        else:
            ctx.logger.log(ROLE, "no_sources", reason="no web source could be retrieved "
                           "(no network, blocked, or the budget was used up)",
                           errors=(research.errors[-5:] if research else None))

        sourced = [s for s in signals if s["url"]]
        claims = [Claim(f"Problem statement: {problem}", None),
                  Claim(f"Target user: {target_user}", None)]
        claims += [Claim(f"Pain signal ({s['type']}): {s['text']}", s["url"]) for s in signals]
        content = self._markdown(idea, problem, target_user, queries, signals, alternatives,
                                 research.errors if research else [], bool(ctx.options.get("pivot_idea")))
        data = {"idea": idea, "problem_statement": problem, "target_user": target_user, "queries": queries,
                "pain_signals": signals, "alternatives": alternatives,
                "sourced_signal_count": len(sourced), "signal_domains": domains(sourced),
                "search_errors": (research.errors[-8:] if research else [])}
        return StepResult(self.phase, self.step, self.name, self.agent, content, claims, data,
                          model, 0, tokens, "WORKING")

    def _signals(self, ctx, raw):
        signals, index_map = [], {}
        for i, item in enumerate(raw[:8], start=1):
            text = safe_text(ctx, ROLE, item.get("summary"), "pain_signal")
            if not text:
                continue
            url = resolve_source(ctx, ROLE, item.get("source"), text)
            url, overlap, kind = verify_signal(ctx, text, url)
            index_map[i] = len(signals)
            signals.append({"text": text, "url": url, "domain": domain_of(url) if url else None,
                            "type": item.get("type"), "mocked": False, "overlap": overlap,
                            "source_kind": kind if url else None})
        return signals, index_map

    def _alternatives(self, ctx, raw, index_map):
        out = []
        for item in raw[:3]:
            name = safe_text(ctx, ROLE, item.get("name"), "alternative")
            desc = safe_text(ctx, ROLE, item.get("description"), "alternative")
            if not name or not desc:
                continue
            verdict = guardrails.check_brief(f"{name}. {desc}")
            if not verdict.allowed:
                ctx.logger.log("guardrail", "refuse_alternative", outcome="blocked", category=verdict.category,
                               reason=verdict.reason, alternative=name)
                continue
            ids = [index_map[i] for i in item.get("signal_ids", []) if i in index_map]
            out.append({"name": name, "description": desc,
                        "why_fits": safe_text(ctx, ROLE, item.get("why_fits"), "alternative") or "",
                        "signal_indexes": sorted(set(ids))})
        return out

    @staticmethod
    def _markdown(idea, problem, target_user, queries, signals, alternatives, errors, pivoted):
        lines = ["# Phase 1, step 1: opportunities", ""]
        if pivoted:
            lines += ["This run PIVOTED at the Phase 1 gate: the idea below is the alternative chosen there.", ""]
        lines += [f"- Idea studied: {idea}", f"- Problem statement (ASSUMPTION, written by the model): {problem}",
                  f"- Target user (ASSUMPTION): {target_user}",
                  f"- Search queries: {'; '.join(queries) or 'none'}", "", "## Pain signals", ""]
        sourced = [s for s in signals if s["url"]]
        if signals:
            lines += ["| # | Type | Signal | Source |", "|---|---|---|---|"]
            for i, s in enumerate(signals, 1):
                src = (f"[{s['domain']}]({s['url']}) ({s.get('source_kind') or 'retrieved'})" if s["url"]
                       else "ASSUMPTION (source not retrieved, or its text does not match the signal)")
                lines.append(f"| {i} | {s['type']} | {md_escape(s['text'])} | {src} |")
        else:
            lines.append("No pain signal was found in the retrieved sources.")
        lines += ["", f"Sourced pain signals: {len(sourced)} from {len(domains(sourced))} domain(s).", ""]
        if errors:
            lines += ["Some search providers returned nothing: " + md_escape("; ".join(errors[-4:])), ""]
        lines += ["## Alternative digital opportunities", ""]
        if alternatives:
            for a in alternatives:
                support = ", ".join(f"#{i + 1}" for i in a["signal_indexes"]) or "none"
                lines.append(f"- **{md_escape(a['name'])}**: {md_escape(a['description'])} "
                             f"Fit: {md_escape(a['why_fits'])} Supporting signals: {support}.")
        else:
            lines.append("- None proposed.")
        return "\n".join(lines)
