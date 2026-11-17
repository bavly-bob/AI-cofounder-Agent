"""Gate cards and the approval inbox. Only a founder click records a decision."""
from __future__ import annotations

import streamlit as st

from approval import GATE_OUTCOMES, ApprovalError
from ui import engine_adapter
from ui.labels import badge

KIND_TEXT = {
    "publish": "Publish (MOCKED executor: nothing is actually published)",
    "send": "Send (MOCKED executor: no message is sent)",
    "spend": "Spend (MOCKED executor: no money moves)",
    "sign": "Sign (MOCKED executor: nothing is signed)",
}


def _submit(run_id, item_id, approved, founder_outcome=None, note=""):
    try:
        with st.spinner("Recording your decision and continuing the run..."):
            engine_adapter.submit_decision(run_id, item_id, approved, founder_outcome, note)
        st.session_state["flash"] = "Decision recorded."
    except ApprovalError as e:
        st.session_state["flash_warning"] = str(e)
    except Exception as e:
        st.session_state["flash_warning"] = f"The decision could not be recorded: {e}"


def gate_card(state, item) -> None:
    d = item.details or {}
    rec = item.recommendation
    with st.container(border=True):
        st.markdown(f"### Gate decision needed: `{d.get('gate', 'gate')}` after step "
                    f"{d.get('phase', '?')}.{d.get('step', '?')}")
        basis = d.get("basis")
        label = f" ({basis})" if basis in ("STUBBED", "MOCKED") else ""
        st.markdown(f"System recommendation: {badge(rec)}{label}", unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        c1.markdown(f"**Threshold (set before the step ran)**  \n{d.get('target') or 'n/a'}")
        c2.markdown(f"**Actual**  \n{d.get('actual') or 'n/a'}")
        if d.get("reasons"):
            st.markdown("**Reasons**")
            for r in d["reasons"]:
                st.markdown(f"- {r}")
        if d.get("change_conditions") and rec != "go":
            st.markdown("**What would change this decision**")
            for c in d["change_conditions"]:
                st.markdown(f"- {c}")
        if not d:
            st.markdown(f"```\n{item.summary}\n```")
        st.caption("The system never upgrades a STOP by itself. Choosing a different outcome is an "
                   "override: it needs a note and is logged with both outcomes.")
        b1, b2 = st.columns(2)
        if b1.button(f"Approve recommendation ({(rec or '').upper()})", key=f"approve_{item.id}", type="primary"):
            _submit(state.run_id, item.id, True)
            st.rerun()
        if b2.button("Reject (halt the run)", key=f"reject_{item.id}"):
            _submit(state.run_id, item.id, False)
            st.rerun()
        with st.expander("Override the recommendation"):
            others = [o for o in (item.options or GATE_OUTCOMES) if o != rec]
            outcome = st.selectbox("Your outcome", others, key=f"override_outcome_{item.id}")
            note = st.text_area("Why (required)", key=f"note_{item.id}")
            if st.button("Override", key=f"override_{item.id}"):
                if not (note or "").strip():
                    st.session_state["flash_warning"] = "An override needs a note explaining why."
                else:
                    _submit(state.run_id, item.id, True, outcome, note)
                st.rerun()


def inbox_card(state, item) -> None:
    with st.container(border=True):
        st.markdown(f"### Approval needed: {KIND_TEXT.get(item.kind, item.kind)}")
        st.markdown(f"**What would happen:** {item.summary}")
        st.markdown("**Details**")
        st.code(item.payload_preview or "(none)", language="json")
        st.caption("If nobody answers, the answer is No. Executors are MOCKED.")
        b1, b2 = st.columns(2)
        if b1.button("Approve", key=f"approve_{item.id}", type="primary"):
            _submit(state.run_id, item.id, True)
            st.rerun()
        if b2.button("Reject", key=f"reject_{item.id}"):
            _submit(state.run_id, item.id, False)
            st.rerun()


def render(state) -> None:
    st.subheader("4. Gate decisions and approval inbox")
    item = state.pending
    if item is None or item.decided:
        st.caption("Nothing is waiting for you.")
        return
    if item.is_gate:
        gate_card(state, item)
    else:
        inbox_card(state, item)
