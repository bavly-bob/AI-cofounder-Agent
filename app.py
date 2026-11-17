"""M1: the engine runs from the command line. This page only points to it and checks the model."""
import os

import streamlit as st

from llm import generate

st.title("AI Cofounder")
st.header("M1 engine — run CLI")
st.caption(
    "The pipeline steps are STUBBED; publish, send, payments and delivery are MOCKED. "
    "Gates, founder approval, the decision log and guardrails work."
)
st.code("python orchestrator.py --brief examples/briefs/weak_idea.json --metric 12", language="bash")
st.write(f"Configured model: `{os.getenv('OLLAMA_MODEL', 'qwen3:8b')}`")

if st.button("Test model"):
    with st.spinner("Asking the model..."):
        result = generate("Reply with one short sentence: hello.")
    if result.get("error"):
        st.error(result["error"])
    else:
        st.success(result["text"])
