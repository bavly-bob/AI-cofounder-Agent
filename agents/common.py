"""Helpers shared by the Phase 1 agents."""
from __future__ import annotations

import re

import guardrails
from steps import load_prompt
from tools.research import UNTRUSTED_RULE, domain_of

_SECTION = re.compile(r"^## (\w+)\s*$", re.M)


def prompt_sections(role: str) -> dict:
    """prompts/<role>.md split by its '## name' headings."""
    text = load_prompt(role)
    parts = _SECTION.split(text)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def fill(template: str, **values) -> str:
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


def system_prompt(sections: dict) -> str:
    return sections["system"] + "\n" + UNTRUSTED_RULE


def brief_text(brief, idea: str | None = None) -> str:
    """A compact brief for prompts (short prompts are faster on a local model)."""
    def val(x, unit=""):
        return "unknown (ASSUMPTION)" if x in (None, "") else f"{x:g}{unit}" if isinstance(x, float) else str(x)
    return "\n".join([
        f"Idea: {idea or brief.idea}",
        f"Target market: {brief.target_market}",
        f"Skills: {', '.join(brief.skills) or 'unknown'}",
        f"Budget: {val(brief.budget_usd, ' USD')}; time: {val(brief.time_per_week_hours, ' h/week')}",
        f"Values: {', '.join(brief.values) or 'unknown'}",
    ])


def current_idea(ctx) -> str:
    """The idea under study: the brief's idea, or the alternative chosen at a Phase 1 PIVOT."""
    return ctx.options.get("pivot_idea") or ctx.brief.idea


def resolve_source(ctx, agent: str, ref, what: str) -> str | None:
    """A cited source id/URL -> a retrieved URL. Anything not retrieved is downgraded and logged."""
    if ctx.research is None:
        return None
    url = ctx.research.registry.resolve(ref)
    if url is None and str(ref or "").strip():
        ctx.logger.log(agent, "downgrade_to_assumption", cited=str(ref)[:300], claim=str(what)[:300],
                       reason="the cited source was not retrieved in this run")
    return url


def safe_text(ctx, agent: str, text: str, where: str) -> str | None:
    """Run the output guardrail on one piece of text. Blocked text is dropped and logged."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return None
    verdict = guardrails.check_output(text)
    if not verdict.allowed:
        ctx.logger.log("guardrail", "drop_blocked_text", outcome="blocked", category=verdict.category,
                       agent_refused=agent, where=where, text=text[:300])
        return None
    return text


def sourced_signals(ctx) -> list:
    """Distinct pain signals from the latest step 1.1 whose URL was actually retrieved.

    Distinct means one signal per page: several signals citing the same URL count once."""
    result = ctx.latest(1, 1)
    signals = ((result.data or {}).get("pain_signals") or []) if result else []
    registry = ctx.research.registry if ctx.research is not None else None
    seen, out = set(), []
    for s in signals:
        url = s.get("url")
        if not url or (registry is not None and not registry.has(url)) or s.get("mocked"):
            continue
        if url not in seen:
            seen.add(url)
            out.append(s)
    return out


def domains(signals) -> list:
    return sorted({domain_of(s["url"]) for s in signals if s.get("url")})


_STOP = {"the", "and", "for", "with", "that", "this", "but", "are", "was", "were", "has", "have", "had", "his",
         "her", "its", "their", "they", "them", "from", "into", "only", "one", "who", "what", "when", "which",
         "you", "your", "our", "not", "all", "any", "can", "will", "would", "about", "says", "said", "still",
         "because", "each", "every", "own", "very", "just", "also", "than", "then", "there", "here", "some"}


def content_words(text) -> set:
    """Lower-case words of 3+ letters, a trailing 's' dropped, common words removed."""
    words = re.findall(r"[^\W\d_]{3,}", str(text or "").lower())
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words} - _STOP


def word_overlap(claim, source_text) -> float:
    """Share of the claim's content words that also appear in the source text (0..1)."""
    mine = content_words(claim)
    if not mine:
        return 0.0
    return len(mine & content_words(source_text)) / len(mine)


def md_escape(text) -> str:
    return str(text or "").replace("|", "/").replace("\n", " ").strip()
