"""AI Cofounder: the founder's screen.

    streamlit run app.py      (or: docker compose up, then open http://localhost:8501)

The founder enters a brief, starts a run, and approves or rejects every gate and
guarded action here. Nothing is approved without a click. Engine calls go through
``ui/engine_adapter.py`` only.
"""
from __future__ import annotations

import streamlit as st

from ui.labels import MOCKED_BANNER, PROJECTOR_CSS

st.set_page_config(page_title="AI Cofounder", layout="wide")


def _safe(title: str, fn, *args):
    """Run one section; a failure shows a plain message, never a stack trace."""
    try:
        return fn(*args)
    except Exception as e:
        st.error(f"{title} could not be shown right now ({type(e).__name__}: {e}). "
                 "The rest of the app still works; try again or start a new run.")
        return None


def _flash() -> None:
    if st.session_state.get("flash"):
        st.success(st.session_state.pop("flash"))
    if st.session_state.get("flash_warning"):
        st.warning(st.session_state.pop("flash_warning"))


def _start(brief, options) -> None:
    from ui import engine_adapter
    try:
        with st.spinner("Running until the next founder decision..."):
            run_id = engine_adapter.start_run(brief, options)
            st.session_state["run_id"] = run_id
            engine_adapter.step_until_pause(run_id)
        st.session_state["flash"] = f"Run {run_id} started."
    except Exception as e:
        st.session_state["flash_warning"] = f"The run could not start: {e}"


def _run_section(options) -> None:
    from ui import decisions, engine_adapter, run_view
    run_id = st.session_state.get("run_id")
    if not run_id:
        st.caption("No run yet. Load a brief above and press Start run.")
        return
    state = engine_adapter.get_state(run_id)
    if state.status == "running":
        state = engine_adapter.step_until_pause(run_id, wait_seconds=2)
    st.markdown(f"Run `{run_id}`")
    if state.status == "running" and engine_adapter.is_live(run_id):
        st.info("The agents are still working.")
        st.button("Refresh", key="btn_refresh")
    tabs = st.tabs(["Run", "Decision log", "Outputs"])
    with tabs[0]:
        _safe("The gate card", decisions.render, state)
        _safe("The timeline", run_view.timeline, state)
        _safe("The step outputs", run_view.step_outputs, state)
    with tabs[1]:
        _safe("The decision log", run_view.decision_log_tab, state)
    with tabs[2]:
        _safe("The output files", run_view.outputs_tab, state)


def main() -> None:
    st.markdown(PROJECTOR_CSS, unsafe_allow_html=True)
    st.title("AI Cofounder")
    st.warning(MOCKED_BANNER)
    from ui import ask, brief_form, sidebar

    options = _safe("The sidebar", sidebar.render) or {}
    if options.get("model_state") == "loading":
        st.info("Model loading: the local model is still downloading. You can prepare the brief now; "
                "the agents start answering once it is ready.")
    _flash()

    brief = _safe("The brief form", brief_form.render)
    if st.button("Start run", key="btn_start", type="primary", disabled=brief is None):
        _start(brief, options)
        st.rerun()
    _safe("The run", _run_section, options)
    st.divider()
    _safe("Ask the agents", ask.render, options.get("model_state") == "ready")


try:
    main()
except Exception as e:  # last line of defence
    st.error(f"Something went wrong ({type(e).__name__}). Please refresh the page.")
