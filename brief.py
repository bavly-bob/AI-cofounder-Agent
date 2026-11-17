"""The founder brief: what the founder wants to build and what they bring."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

UNKNOWN = "ASSUMPTION: unknown"


class BriefError(ValueError):
    """The brief file is missing, unreadable or has no idea in it."""


@dataclass
class FounderBrief:
    idea: str
    skills: list = field(default_factory=list)
    budget_usd: float | None = None          # None => unknown (ASSUMPTION)
    time_per_week_hours: float | None = None  # None => unknown (ASSUMPTION)
    target_market: str = UNKNOWN
    values: list = field(default_factory=list)
    notes: str = ""

    def as_text(self) -> str:
        """Every free-text field as one string (what the guardrails read):
        idea, notes, target market, skills and values."""
        parts = [self.idea, self.notes, "" if self.target_market == UNKNOWN else self.target_market,
                 *[str(s) for s in self.skills], *[str(v) for v in self.values]]
        return "\n".join(p for p in parts if p)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "FounderBrief":
        """Build a brief from loosely shaped data. Missing fields become unknown."""
        return cls(
            idea=_text(data.get("idea")),
            skills=_str_list(data.get("skills")),
            budget_usd=_number(data.get("budget_usd")),
            time_per_week_hours=_number(data.get("time_per_week_hours")),
            target_market=_text(data.get("target_market")) or UNKNOWN,
            values=_str_list(data.get("values")),
            notes=_text(data.get("notes")),
        )


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value).strip()


def _str_list(value) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[,;\n]", value) if p.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group()) if match else None


def load_brief(path) -> FounderBrief:
    """Read a brief from a JSON file. Raises BriefError with a plain message on any problem."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise BriefError(f"Could not read the brief file {path}: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise BriefError(f"The brief file {path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise BriefError(f"The brief file {path} must hold a JSON object")
    brief = FounderBrief.from_dict(data)
    if not brief.idea:
        raise BriefError(f"The brief file {path} has no 'idea'")
    return brief


def fallback_brief(text: str) -> FounderBrief:
    """Everything goes in ``idea``; the rest is unknown and labelled ASSUMPTION."""
    return FounderBrief(
        idea=str(text or "").strip(),
        target_market=UNKNOWN,
        notes="ASSUMPTION: skills, budget, time per week, target market and values are unknown "
        "(the free text could not be structured).",
    )


_PROMPT = """Extract a founder brief from the text below. Reply with ONE JSON object and nothing else.
Keys: "idea" (string), "skills" (list of strings), "budget_usd" (number or null),
"time_per_week_hours" (number or null), "target_market" (string or null),
"values" (list of strings), "notes" (string).
Use null or [] for anything the text does not say. Do not invent facts.

TEXT:
"""


def _extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the model reply")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("model reply is not a JSON object")
    return data


def parse_brief_text(text, generate=None, logger=None) -> FounderBrief:
    """Turn free text into a brief with the local model. Never raises.

    If the model is unreachable or replies with something unusable, the whole
    text becomes ``idea`` and the other fields are marked unknown (ASSUMPTION).
    """
    text = "" if text is None else str(text)
    try:
        if generate is None:
            from llm import generate  # imported here so a broken client cannot break the fallback
        reply = generate(_PROMPT + text)
        if logger is not None:
            try:
                logger.log(
                    "brief_parser", "llm_call", model=reply.get("model"), cost=reply.get("cost", 0) or 0,
                    tokens=reply.get("tokens"), error=reply.get("error"),
                )
            except Exception:
                pass
        if reply.get("error") or not reply.get("text"):
            return fallback_brief(text)
        brief = FounderBrief.from_dict(_extract_json(reply["text"]))
        if not brief.idea:
            brief.idea = text.strip()
        return brief
    except Exception:
        return fallback_brief(text)
