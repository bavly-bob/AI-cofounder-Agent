"""Founder brief: pick an example, paste text or upload JSON; then check and edit every field."""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from brief import UNKNOWN, FounderBrief, parse_brief_text

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = ROOT / "examples" / "briefs"

FIELDS = (  # (key, label, kind)
    ("idea", "Idea", "area"),
    ("target_market", "Target market", "text"),
    ("skills", "Skills (comma separated)", "text"),
    ("budget_usd", "Budget (USD)", "text"),
    ("time_per_week_hours", "Time per week (hours)", "text"),
    ("values", "Values (comma separated)", "text"),
    ("notes", "Notes", "area"),
)


def examples() -> dict:
    try:
        return {p.stem: p for p in sorted(EXAMPLES_DIR.glob("*.json"))}
    except OSError:
        return {}


def _to_fields(brief: FounderBrief) -> dict:
    def num(v):
        return "" if v is None else f"{v:g}"
    return {
        "idea": brief.idea,
        "target_market": "" if brief.target_market == UNKNOWN else brief.target_market,
        "skills": ", ".join(brief.skills),
        "budget_usd": num(brief.budget_usd),
        "time_per_week_hours": num(brief.time_per_week_hours),
        "values": ", ".join(brief.values),
        "notes": brief.notes,
    }


def _load_into_form(brief: FounderBrief, source: str) -> None:
    for key, value in _to_fields(brief).items():
        st.session_state[f"bf_{key}"] = value
    st.session_state["bf_source"] = source


def _on_use_example():
    name = st.session_state.get("bf_example")
    path = examples().get(name)
    if path is None:
        st.session_state["bf_error"] = "That example could not be found."
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        _load_into_form(FounderBrief.from_dict(data if isinstance(data, dict) else {}), f"example: {name}")
        st.session_state.pop("bf_error", None)
    except (OSError, ValueError) as e:
        st.session_state["bf_error"] = f"The example could not be read: {e}"


def _on_parse_text():
    text = (st.session_state.get("bf_paste") or "").strip()
    if not text:
        st.session_state["bf_error"] = "Paste a few sentences about your idea first."
        return
    # Uses the local model when it is ready; otherwise the whole text becomes the idea
    # and every other field is marked ASSUMPTION (brief.parse_brief_text never raises).
    _load_into_form(parse_brief_text(text), "pasted text")
    st.session_state.pop("bf_error", None)


def _on_upload():
    upload = st.session_state.get("bf_upload")
    if upload is None:
        st.session_state["bf_error"] = "Choose a JSON file first."
        return
    try:
        data = json.loads(upload.getvalue().decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("the file must hold one JSON object")
        brief = FounderBrief.from_dict(data)
        if not brief.idea:
            raise ValueError("the file has no 'idea'")
        _load_into_form(brief, f"upload: {upload.name}")
        st.session_state.pop("bf_error", None)
    except (ValueError, UnicodeDecodeError) as e:
        st.session_state["bf_error"] = f"That file is not a usable brief: {e}"


def current_brief() -> FounderBrief | None:
    """The brief as edited in the form, or None when there is no idea yet."""
    data = {key: st.session_state.get(f"bf_{key}", "") for key, _, _ in FIELDS}
    if not str(data.get("idea") or "").strip():
        return None
    for key in ("budget_usd", "time_per_week_hours"):
        if not str(data[key]).strip():
            data[key] = None
    return FounderBrief.from_dict(data)


def render() -> FounderBrief | None:
    st.subheader("1. Founder brief")
    how = st.radio("Start from", ["Example", "Paste text", "Upload JSON"], horizontal=True, key="bf_how")
    if how == "Example":
        names = list(examples())
        if names:
            st.selectbox("Example brief", names, key="bf_example")
            st.button("Use this example", key="btn_use_example", on_click=_on_use_example)
        else:
            st.info("No example briefs were found in examples/briefs/.")
    elif how == "Paste text":
        st.text_area("Describe your idea, skills, budget, time and market", key="bf_paste", height=120)
        st.button("Read my text", key="btn_parse", on_click=_on_parse_text)
    else:
        st.file_uploader("Brief (.json)", type=["json"], key="bf_upload")
        st.button("Load this file", key="btn_upload", on_click=_on_upload)
    if st.session_state.get("bf_error"):
        st.warning(st.session_state["bf_error"])

    if st.session_state.get("bf_source"):
        st.caption(f"Loaded from {st.session_state['bf_source']}. Check and edit before you start.")
    cols = st.columns(2)
    for i, (key, label, kind) in enumerate(FIELDS):
        col = cols[0] if kind == "area" or i % 2 == 0 else cols[1]
        widget = col.text_area if kind == "area" else col.text_input
        value = widget(label, key=f"bf_{key}")
        if not str(value or "").strip() and key != "notes":
            col.caption(":orange[ASSUMPTION: unknown — fill it in if you know it]")
        elif "ASSUMPTION" in str(value):
            col.caption(":orange[Contains an ASSUMPTION]")
    return current_brief()
