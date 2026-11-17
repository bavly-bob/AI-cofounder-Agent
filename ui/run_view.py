"""Timeline of the six phases, step outputs, decision log and output files of one run."""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from ui.labels import LIVE_PHASES, PHASES, STATUS_TEXT, badge

PAYMENT_TEST = "outputs/payment_test_log.md (written by `pytest tests/test_sale_to_cash.py`)"


def _gate_line(g: dict) -> str:
    system, founder = g.get("system_outcome"), g.get("founder_outcome")
    text = f"Gate `{g.get('gate')}` after {g.get('after')}: system {badge(system)}"
    if g.get("basis") in ("STUBBED", "MOCKED"):
        text += f" ({g['basis']})"
    if founder and founder != system:
        text += f" &rarr; **OVERRIDE** by the founder: {badge(founder)}"
    elif founder and g.get("decision") == "rejected":
        text += " &rarr; rejected by the founder (run halted)"
    elif founder:
        text += " &rarr; confirmed by the founder"
    else:
        text += " &rarr; waiting for the founder"
    return text


def timeline(state) -> None:
    st.subheader("2. Timeline")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Status", STATUS_TEXT.get(state.status, state.status))
    c2.metric("Tokens", f"{int(state.tokens or 0):,}")
    c3.metric("Cost", f"${float(state.cost or 0):.2f}")
    c4.metric("Wall time", f"{state.wall_seconds or 0:.0f} s")
    if state.message:
        st.info(state.message)
    rows = [st.columns(3), st.columns(3)]
    for n, title in PHASES.items():
        col = rows[(n - 1) // 3][(n - 1) % 3]
        with col.container(border=True):
            st.markdown(f"**Phase {n}: {title}**")
            if n in LIVE_PHASES:
                steps = [s for s in state.steps if int(s.get("phase", 0) or 0) == n]
                gates = [g for g in state.gates if str(g.get("after", "")).startswith(f"{n}.")]
                if not steps and not gates:
                    st.caption("Not reached yet.")
                for s in steps:
                    secs = s.get("seconds")
                    timing = f", {secs:.1f} s" if isinstance(secs, (int, float)) else ""
                    st.markdown(f"Step {s.get('id')} `{s.get('name')}`: {s.get('status')}{timing}")
                for g in gates:
                    st.markdown(_gate_line(g), unsafe_allow_html=True)
            else:
                st.markdown("STUBBED — see ARCHITECTURE.md")
                if n == 5:
                    st.markdown(f"Sale to cash: **MOCKED** (no real money). Payment test: {PAYMENT_TEST}")
    if state.final_outcome:
        st.markdown(f"Final outcome: {badge(state.final_outcome)}", unsafe_allow_html=True)
        for reason in state.stop_reasons or []:
            st.markdown(f"- {reason}")


def _run_files(state) -> list:
    try:
        return sorted(p for p in state.run_dir.iterdir() if p.is_file() and not p.name.startswith("."))
    except OSError:
        return []


def step_outputs(state) -> None:
    st.subheader("3. Step outputs")
    files = [p for p in _run_files(state) if p.suffix in (".md", ".html") and p.name.startswith("phase")]
    if not files:
        st.caption("No step output yet.")
    for path in files:
        with st.expander(path.name, expanded=False):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                st.warning("This file could not be read.")
                continue
            if path.suffix == ".html":
                st.markdown('<span class="mocked">MOCKED landing page — not published</span>',
                            unsafe_allow_html=True)
                components.html(text, height=420, scrolling=True)
            else:
                st.markdown(text)


def decision_log_tab(state) -> None:
    path = Path(state.run_dir) / "decision_log.json"
    try:
        raw = path.read_text(encoding="utf-8")
        entries = json.loads(raw or "[]")
    except FileNotFoundError:
        st.caption("No decision log yet.")
        return
    except (OSError, ValueError):
        st.warning("The decision log could not be read right now. Try again in a moment.")
        return
    rows = [{
        "time": e.get("timestamp"), "agent": e.get("agent"), "action": e.get("action"),
        "outcome": e.get("outcome") or e.get("decision") or "", "approved_by": e.get("approved_by") or "",
        "model": e.get("model") or "", "cost": e.get("cost"), "sources": len(e.get("sources") or []),
    } for e in entries if isinstance(e, dict)]
    st.caption(f"{len(rows)} entries for run {state.run_id}")
    st.dataframe(rows, hide_index=True)
    st.download_button("Download decision_log.json", raw, file_name=f"decision_log_{state.run_id}.json",
                       mime="application/json", key="dl_log")


def outputs_tab(state) -> None:
    files = _run_files(state)
    if not files:
        st.caption("No files yet.")
    for i, path in enumerate(files):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        st.download_button(f"Download {path.name}", data, file_name=path.name, key=f"dl_file_{i}")
