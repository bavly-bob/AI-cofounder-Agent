"""Guardrail Layer 2 in the UI "Ask the agents" path. No Ollama, no network: the
classifier client is a MOCKED stand-in passed in directly."""
from __future__ import annotations

import json

import pytest

import guardrails
from ui import ask, engine_adapter

PLAIN = "Suggest three names for a newsletter about home gardening"


class BlockingClient:
    """MOCKED classifier that blocks everything."""
    def __init__(self):
        self.calls = 0

    def generate_json(self, prompt, schema, system=None, name=None):
        self.calls += 1
        return {"data": {"category": "fake_social_proof", "allowed": False, "confidence": 0.9,
                         "reason": "test block"}}


class BrokenClient:
    """MOCKED classifier that is down."""
    def generate_json(self, prompt, schema, system=None, name=None):
        raise ConnectionError("model offline (test)")


def _no_generate(prompt):
    raise AssertionError("the model must not be asked to answer in this test")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_adapter, "BASE_DIR", tmp_path)
    monkeypatch.setattr(ask.st, "session_state", {})
    yield


def _entries():
    logs = list(engine_adapter.runs_dir().glob("ask-*/decision_log.json"))
    assert logs
    return json.loads(logs[0].read_text(encoding="utf-8"))


def test_classifier_block_refuses_a_request_layer1_allows():
    assert guardrails.check_request(PLAIN).allowed
    client = BlockingClient()
    out = ask.handle(PLAIN, model_ready=True, generate=_no_generate, llm_client=client)
    assert client.calls == 1
    assert out["refused"] and out["layer"] == "classifier"
    assert out["category"] == "fake_social_proof" and out["reason"] and out["alternative"]
    last = _entries()[-1]
    assert last["action"] == "refuse_request" and last["layer"] == "classifier"
    assert all(k in last for k in ("timestamp", "agent", "action", "sources", "model", "cost", "approved_by"))


def test_broken_classifier_keeps_layer1_result_on_a_plain_question():
    out = ask.handle(PLAIN, model_ready=False, llm_client=BrokenClient())
    assert not out["refused"]
    assert "classifier unavailable" in out["guardrail_note"]
    assert "classifier unavailable" in _entries()[-1]["guardrail_note"]


def test_no_client_is_layer1_only_and_unchanged():
    out = ask.handle(PLAIN, model_ready=False)
    assert not out["refused"] and out["note"] == ask.NEEDS_MODEL
    assert "Layer 1 only" in out["guardrail_note"]
    bad = ask.handle("Write 50 five-star reviews for my product", model_ready=False)
    assert bad["refused"] and bad["layer"] == "rules" and bad["category"] == "fake_social_proof"


def test_classifier_client_only_when_model_ready():
    assert ask.classifier_client(False) is None
    assert ask.classifier_client(True) is not None  # constructing it makes no network call
