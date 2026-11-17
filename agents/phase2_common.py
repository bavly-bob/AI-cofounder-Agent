"""Helpers shared by the Phase 2 agents."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOCKED_EVIDENCE = "MOCKED evidence — demonstration only"


def load_phase2() -> dict:
    return json.loads((ROOT / "config" / "thresholds.json").read_text(encoding="utf-8"))["phase2"]


def evidence_is_mocked(ctx) -> bool:
    """True when this run's demand evidence is the --mock-signups demo input (and nothing better)."""
    o = ctx.options
    if o.get("waitlist_path") or o.get("signups") is not None:
        return False
    return o.get("mock_signups") is not None


def mocked_banner(ctx) -> list:
    """A visible line for every Phase 2 artifact of a run that uses MOCKED evidence."""
    if not evidence_is_mocked(ctx):
        return []
    return [f"**{MOCKED_EVIDENCE}.** This run uses typed-in sign-up numbers (`--mock-signups`), "
            "not real people. Nothing in it is a real validation.", ""]


def phase1_findings(ctx) -> tuple:
    """(text block for a prompt, list of retrieved URLs) from the saved Phase 1 results."""
    lines, urls = [], []
    reg = ctx.research.registry if ctx.research is not None else None

    def sid(url):
        entry = reg.get(url) if reg is not None and url else None
        return entry["id"] if entry else None

    opp = ctx.latest(1, 1)
    d = (opp.data if opp else {}) or {}
    if d.get("problem_statement"):
        lines.append(f"Problem (ASSUMPTION): {d['problem_statement']}")
    if d.get("target_user"):
        lines.append(f"Target user (ASSUMPTION): {d['target_user']}")
    for s in (d.get("pain_signals") or [])[:6]:
        ref = sid(s.get("url"))
        lines.append(f"Pain signal: {s.get('text')}" + (f" [{ref}]" if ref else " [ASSUMPTION]"))
        if ref:
            urls.append(s["url"])
    market = ctx.latest(1, 3)
    m = (market.data if market else {}) or {}
    for key in ("segment", "beachhead", "use_case"):
        if m.get(key):
            lines.append(f"{key.replace('_', ' ').capitalize()} (ASSUMPTION): {m[key]}")
    for c in (m.get("competitors") or [])[:4]:
        ref = sid(c.get("url"))
        lines.append(f"Competitor: {c.get('name')} ({c.get('note')})" + (f" [{ref}]" if ref else ""))
        if ref:
            urls.append(c["url"])
    return "\n".join(lines) or "(no Phase 1 findings were saved)", urls
