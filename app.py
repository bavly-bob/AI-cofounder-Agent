"""M0 skeleton: proves the app can talk to the local model. No pipeline yet."""
import os

import streamlit as st

from llm import generate

st.title("AI Cofounder (M0 skeleton)")
st.caption("M0 skeleton: pipeline, gates and approval are STUBBED; payments and delivery are MOCKED.")
st.write(f"Configured model: `{os.getenv('OLLAMA_MODEL', 'qwen3:8b')}`")

if st.button("Test model"):
    with st.spinner("Asking the model..."):
        result = generate("Reply with one short sentence: hello.")
    if result.get("error"):
        st.error(result["error"])
    else:
        st.success(result["text"])
