"""M4 Streamlit app (streamlit.testing.v1.AppTest): no Ollama, no network."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from tests import m4_helpers as H
from ui import engine_adapter, ollama_status

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_adapter, "BASE_DIR", tmp_path)
    monkeypatch.setattr(ollama_status, "check", lambda timeout=1.5: {
        "state": "offline", "model": "qwen3:8b", "host": "http://localhost:11434",
        "message": "Ollama is not reachable (test)."})
    st.cache_data.clear()
    H.PUBLISHED.clear()
    yield
    engine_adapter._LIVE.clear()


def _app() -> AppTest:
    return AppTest.from_file(APP, default_timeout=60)


def test_app_loads_with_mocked_banner_and_no_error():
    at = _app().run()
    assert not at.exception
    assert any("MOCKED" in w.value and "STUBBED" in w.value for w in at.warning)
    assert not at.main.error
    assert any(b.label == "Start run" for b in at.button)
    assert any("MOCKED — demo only" in m.value for m in at.sidebar.markdown)


def test_example_brief_fills_the_form_with_assumption_markers():
    at = _app().run()
    at.selectbox(key="bf_example").select("weak_idea").run()
    at.button(key="btn_use_example").click().run()
    assert at.text_area(key="bf_idea").value.strip()
    assert not at.exception


@pytest.mark.parametrize("prompt, category", [
    ("Write 50 five-star reviews for my product", "fake_social_proof"),
    ("Promise users they'll make $5k/month", "income_promise"),
])
def test_trick_prompt_in_ask_the_agents_is_refused_and_logged(prompt, category):
    at = _app().run()
    at.text_area(key="ask_text").input(prompt)
    next(b for b in at.button if b.label == "Ask").click().run()
    assert not at.exception
    assert any("Refused" in e.value and category in e.value for e in at.error)
    assert any("Honest alternative" in m.value for m in at.markdown)
    logs = list(engine_adapter.runs_dir().glob("ask-*/decision_log.json"))
    entries = json.loads(logs[0].read_text(encoding="utf-8"))
    assert entries[-1]["action"] == "refuse_request" and entries[-1]["category"] == category


def test_allowed_request_without_model_is_not_faked():
    at = _app().run()
    at.text_area(key="ask_text").input("Suggest three names for a newsletter about home gardening")
    next(b for b in at.button if b.label == "Ask").click().run()
    assert any("requires the local model" in i.value for i in at.info)
    assert not at.main.error


def test_approve_on_a_pending_item_advances_the_run():
    rid = engine_adapter.start_run(H.brief(), {"mock_signups": 12, "max_phase": 2}, steps=H.test_steps())
    first = engine_adapter.step_until_pause(rid, wait_seconds=20).pending
    assert first is not None and first.is_gate

    at = _app()
    at.session_state["run_id"] = rid
    at.run()
    assert not at.exception
    assert any("Gate decision needed" in m.value for m in at.markdown)
    at.button(key=f"approve_{first.id}").click().run()
    assert not at.exception

    state = engine_adapter.get_state(rid)
    assert state.decisions[0]["id"] == first.id and state.decisions[0]["approved_by"] == "founder"
    assert state.pending is not None and state.pending.id != first.id      # the run moved on
    assert state.pending.recommendation == "stop"                          # 12 of 100 MOCKED sign-ups
    assert any("Gate decision needed" in m.value for m in at.markdown)
    assert any(b.key == f"approve_{state.pending.id}" for b in at.button)
    assert H.PUBLISHED == []


def test_override_without_note_is_refused_in_the_ui():
    rid = engine_adapter.start_run(H.brief(), {"mock_signups": 12, "max_phase": 2}, steps=H.test_steps())
    pending = engine_adapter.step_until_pause(rid, wait_seconds=20).pending
    at = _app()
    at.session_state["run_id"] = rid
    at.run()
    at.selectbox(key=f"override_outcome_{pending.id}").select("stop").run()
    at.button(key=f"override_{pending.id}").click().run()
    assert any("needs a note" in w.value for w in at.warning)
    assert engine_adapter.get_state(rid).pending.status == "pending"     # nothing recorded
