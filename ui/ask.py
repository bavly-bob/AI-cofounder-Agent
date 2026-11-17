"""Ask the agents: every request is checked by the guardrails first, and every answer is logged."""
from __future__ import annotations

import streamlit as st

import guardrails
from decision_log import DecisionLogger, new_run_id
from ui import engine_adapter

AGENT = "ask_agents"
NEEDS_MODEL = "Allowed. Answered by agent — requires the local model, which is not ready yet. No answer was made up."
_SYSTEM = ("You are an assistant on a team that helps a founder validate a digital product. Answer briefly and "
           "honestly. Never promise income or success, never invent reviews, testimonials or numbers, and say "
           "plainly when something is an assumption.\n\nQuestion: ")


def _logger() -> DecisionLogger:
    run_id = st.session_state.get("run_id")
    if run_id:
        return DecisionLogger(engine_adapter.runs_dir() / run_id / "decision_log.json", run_id=run_id)
    session = st.session_state.setdefault("ask_session", "ask-" + new_run_id())
    return DecisionLogger(engine_adapter.runs_dir() / session / "decision_log.json", run_id=session)


def _check(fn, text):
    try:
        return fn(text)
    except Exception as e:  # a broken check fails closed
        return guardrails.GuardrailResult(False, None, f"The safety check could not be completed ({e}).", None)


def classifier_client(model_ready: bool):
    """The Layer 2 classifier client: the local model when the read-only Ollama check
    (``ui/ollama_status.py``) says it is ready, otherwise None (Layer 1 rules only)."""
    if not model_ready:
        return None
    try:
        from llm import OllamaClient
        return OllamaClient()
    except Exception:
        return None


def handle(text: str, model_ready: bool, generate=None, llm_client=None) -> dict:
    """Check (Layer 1 rules, then the Layer 2 classifier when ``llm_client`` is given),
    (maybe) answer, and log one request. Returns what the UI shows."""
    verdict = _check(lambda t: guardrails.check_request_layered(t, llm_client=llm_client), text)
    logger = _logger()
    layer = getattr(verdict, "layer", None) or ("rules" if not verdict.allowed else None)
    note = getattr(verdict, "note", "") or ""
    if not verdict.allowed:
        logger.log("guardrail", "refuse_request", outcome="refused", category=verdict.category,
                   reason=verdict.reason, safe_alternative=verdict.safe_alternative, source="ask_the_agents",
                   layer=layer, guardrail_note=note)
        return {"text": text, "refused": True, "category": verdict.category, "reason": verdict.reason,
                "alternative": verdict.safe_alternative, "layer": layer, "guardrail_note": note}
    if not model_ready:
        logger.log(AGENT, "answer_request", outcome="not_answered", note="local model not ready",
                   guardrail_note=note)
        return {"text": text, "refused": False, "answer": None, "note": NEEDS_MODEL, "guardrail_note": note}
    if generate is None:
        from llm import generate
    reply = generate(_SYSTEM + text)
    model = reply.get("model")
    if reply.get("error") or not reply.get("text"):
        logger.log(AGENT, "answer_request", model=model, outcome="error", error=reply.get("error"),
                   guardrail_note=note)
        return {"text": text, "refused": False, "answer": None,
                "note": "The local model did not answer. Check that Ollama is running, then try again."}
    out = _check(guardrails.check_output, reply["text"])
    if not out.allowed:
        logger.log("guardrail", "refuse_output", model=model, outcome="blocked", category=out.category,
                   reason=out.reason, safe_alternative=out.safe_alternative, source="ask_the_agents",
                   layer="rules", guardrail_note=note)
        return {"text": text, "refused": True, "category": out.category, "reason": out.reason,
                "alternative": out.safe_alternative}
    logger.log(AGENT, "answer_request", model=model, cost=0, outcome="answered", guardrail_note=note)
    return {"text": text, "refused": False, "answer": reply["text"], "model": model, "guardrail_note": note}


def render(model_ready: bool) -> None:
    st.subheader("Ask the agents")
    st.caption("Every request is checked by the guardrails first (rules, plus the local-model classifier "
               "when Ollama is ready). Refusals are logged.")
    with st.form("ask_form", clear_on_submit=True):
        text = st.text_area("Your request", key="ask_text", height=90)
        sent = st.form_submit_button("Ask")
    if sent and (text or "").strip():
        try:
            st.session_state.setdefault("ask_history", []).insert(0, handle(text.strip(), model_ready,
                                                                    llm_client=classifier_client(model_ready)))
        except Exception as e:
            st.warning(f"The request could not be handled right now: {e}")
    for item in st.session_state.get("ask_history", [])[:5]:
        with st.container(border=True):
            st.markdown(f"**You asked:** {item['text']}")
            if item.get("refused"):
                layer = f", layer: {item['layer']}" if item.get("layer") else ""
                st.error(f"Refused (category: {item.get('category')}{layer}). {item.get('reason')}")
                if item.get("alternative"):
                    st.markdown(f"**Honest alternative:** {item['alternative']}")
            elif item.get("answer"):
                st.markdown(item["answer"])
                st.caption(f"Answered by the local model {item.get('model')}. Check it before you use it.")
            else:
                st.info(item.get("note") or NEEDS_MODEL)
