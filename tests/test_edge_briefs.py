"""M5 robustness: odd founder briefs go through the orchestrator CLI path with no Ollama.

Expected for every brief: a clean STOP (or a request for more information), a
friendly message, a log entry, never a stack trace and never a GO without evidence.
The fixtures in tests/fixtures/briefs_edge/ include trick inputs for our own guardrails.
"""
import json
import socket
from pathlib import Path

import pytest

import orchestrator

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "briefs_edge"
RUN_BRIEFS = ["empty.txt", "long_20k.json", "arabic_only.json", "emoji_only.json", "nonsense.json",
              "prompt_injection.json", "zero_budget_time.json", "disguised_excluded.json"]


def _no_network(*args, **kwargs):
    raise OSError("network disabled in this test (no Ollama, no internet)")


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)   # runs/ goes to a temp folder
    # No Ollama and no internet: every socket connection fails, as on a laptop without them.
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    monkeypatch.setattr("builtins.input", lambda *_: (_ for _ in ()).throw(EOFError()))

    def invoke(name, capsys):
        code = orchestrator.main(["--brief", str(FIXTURES / name)])
        out = capsys.readouterr()
        return code, out.out + out.err

    return invoke


def _logs(tmp_path):
    return [json.loads(p.read_text(encoding="utf-8")) for p in (tmp_path / "runs").glob("*/decision_log.json")]


def test_every_edge_fixture_exists():
    for name in RUN_BRIEFS + ["empty.json"]:
        assert (FIXTURES / name).exists(), name
    assert len((FIXTURES / "long_20k.json").read_text(encoding="utf-8")) >= 20000


@pytest.mark.parametrize("name", RUN_BRIEFS)
def test_edge_brief_stops_cleanly_with_a_log_entry(name, cli, capsys, tmp_path):
    code, text = cli(name, capsys)
    assert "Traceback" not in text
    assert code in (0, 1), text                             # 1 = a step reported an error (e.g. no model)
    assert "Final outcome : STOP" in text, text            # never a GO without evidence
    assert "Why" in text                                    # a plain reason is shown to the founder
    logs = _logs(tmp_path)
    assert len(logs) == 1 and logs[0], "the run must leave a decision log"
    actions = [e["action"] for e in logs[0]]
    assert actions[0] == "start_run" and actions[-1] == "end_run"
    assert not [e for e in logs[0] if e.get("outcome") == "go" and e["agent"] == "gate"]
    assert not [e for e in logs[0] if e["approved_by"] == "founder"]


def test_disguised_excluded_product_is_refused_at_the_brief(cli, capsys, tmp_path):
    code, text = cli("disguised_excluded.json", capsys)
    assert "excluded_product" in text and "Suggested alternative" in text
    refusals = [e for e in _logs(tmp_path)[0] if e["action"] == "refuse_brief"]
    assert refusals and refusals[0]["category"] == "excluded_product"


def test_prompt_injection_brief_is_refused_at_the_brief(cli, capsys, tmp_path):
    code, text = cli("prompt_injection.json", capsys)
    refusals = [e for e in _logs(tmp_path)[0] if e["action"] == "refuse_brief"]
    assert refusals, text
    assert refusals[0]["category"] == "approval_bypass"
    assert "(no step ran)" in text


def test_empty_json_brief_gets_a_friendly_message_and_no_trace(cli, capsys):
    code, text = cli("empty.json", capsys)
    assert "Traceback" not in text
    assert code == 2
    assert "Could not load the brief" in text and "no 'idea'" in text


def test_empty_json_brief_leaves_a_log_entry(cli, capsys, tmp_path):
    cli("empty.json", capsys)
    assert _logs(tmp_path)
