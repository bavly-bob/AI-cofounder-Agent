"""Sidebar: model status, run options, Phase 2 evidence, New run / Resume run."""
from __future__ import annotations

import csv
import io

import streamlit as st

from ui import engine_adapter, ollama_status


@st.cache_data(ttl=10, show_spinner=False)
def _cached_status() -> dict:
    return ollama_status.check()


def model_status() -> dict:
    try:
        return _cached_status()
    except Exception:
        return ollama_status.check()


def _count_csv_rows(upload) -> int | None:
    """Count non-empty data rows. The file is read in memory only; nothing in it is stored."""
    try:
        text = upload.getvalue().decode("utf-8-sig", errors="replace")
        rows = [r for r in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in r)]
        return max(len(rows) - 1, 0)  # minus the header row
    except Exception:
        return None


def render() -> dict:
    """Draw the sidebar and return the run options chosen there."""
    sb = st.sidebar
    status = model_status()
    sb.header("Model")
    sb.markdown(f"**{status['model']}** via Ollama (local, $0.00)")
    {"ready": sb.success, "loading": sb.warning, "offline": sb.error}[status["state"]](status["message"])

    sb.header("Run options")
    fast = sb.toggle("Fast mode (fewer searches)", value=True, key="opt_fast")
    max_phase = sb.radio("Run up to phase", [1, 2], index=1, horizontal=True, key="opt_phase")

    sb.header("Phase 2 evidence")
    upload = sb.file_uploader("Waitlist export (CSV)", type=["csv"], key="opt_csv",
                              help="Only the row count is used. Emails are never stored or shown.")
    rows = _count_csv_rows(upload) if upload is not None else None
    if upload is not None:
        sb.caption(f"{rows} rows counted (counts only)." if rows is not None else "The CSV could not be read.")
    sb.markdown("Founder-reported numbers")
    signups = sb.number_input("Sign-ups (founder-reported)", min_value=0, value=0, step=1, key="opt_signups")
    visitors = sb.number_input("Visitors (founder-reported)", min_value=0, value=0, step=1, key="opt_visitors")
    sb.markdown('<span class="mocked">MOCKED — demo only</span>', unsafe_allow_html=True)
    use_mock = sb.checkbox("Use mock numbers (not real evidence)", key="opt_use_mock")
    mock_signups = sb.number_input("Mock sign-ups", min_value=0, value=12, step=1, key="opt_mock_signups",
                                   disabled=not use_mock)
    mock_visitors = sb.number_input("Mock visitors", min_value=0, value=400, step=1, key="opt_mock_visitors",
                                    disabled=not use_mock)

    sb.header("Runs")
    if sb.button("New run", key="btn_new_run"):
        st.session_state.pop("run_id", None)
        st.session_state.pop("flash", None)
    saved = engine_adapter.saved_runs()
    if saved:
        labels = {r["run_id"]: f"{r['run_id']} ({r['status']}) {r['idea'][:30]}" for r in saved}
        choice = sb.selectbox("Saved runs", list(labels), format_func=labels.get, key="opt_resume_choice")
        if sb.button("Resume run", key="btn_resume"):
            st.session_state["run_id"] = choice
    else:
        sb.caption("No saved runs yet.")

    return {
        "fast": bool(fast),
        "max_phase": int(max_phase),
        "waitlist_rows": rows,
        "signups": int(signups) or None,
        "visitors": int(visitors) or None,
        "mock_signups": int(mock_signups) if use_mock else None,
        "mock_visitors": int(mock_visitors) if use_mock else None,
        "model_state": status["state"],
    }
