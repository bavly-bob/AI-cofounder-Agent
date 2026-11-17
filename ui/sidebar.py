"""Sidebar: model status, run options, Phase 2 evidence, New run / Resume run."""
from __future__ import annotations

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


def _waitlist_counts(upload) -> dict | None:
    """Aggregate counts with the CLI's checks (valid emails, duplicates). Read in memory; nothing is stored."""
    try:
        import orchestrator
        return orchestrator.count_waitlist_bytes(upload.getvalue())
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
    counts = _waitlist_counts(upload) if upload is not None else None
    if upload is not None:
        if counts is not None:
            sb.caption(f"{counts['valid']} unique valid emails in {counts['rows']} rows "
                       f"({counts['duplicates']} duplicates, {counts['invalid_emails']} invalid). Counts only; "
                       "the run counts sign-ups inside the test window.")
        else:
            sb.error("The CSV could not be read. The run will STOP at the Phase 2 gate for this reason; "
                     "the typed sign-ups are NOT used instead.")
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
        "waitlist_upload": None if upload is None else ("uploaded" if counts is not None else "unreadable"),
        "waitlist_bytes": upload.getvalue() if upload is not None and counts is not None else None,
        "waitlist_counts": counts,
        "signups": int(signups) or None,
        "visitors": int(visitors) or None,
        "mock_signups": int(mock_signups) if use_mock else None,
        "mock_visitors": int(mock_visitors) if use_mock else None,
        "model_state": status["state"],
    }
