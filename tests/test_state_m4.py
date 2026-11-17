"""M4 pause/resume: PendingApprover, RunState and the engine adapter (no Ollama, no network)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import approval
from approval import AWAITING, FOUNDER, ApprovalError, PendingApprover, guarded_action
from decision_log import DecisionLogger
from run_state import RunState, RunStateError, list_runs, load
from tests import m4_helpers as H
from ui import engine_adapter


class Ctx:
    def __init__(self, approver, logger):
        self.approver, self.logger = approver, logger


@pytest.fixture
def state(tmp_path):
    return RunState.new("run-test-1", {"idea": "x"}, {}, tmp_path / "runs")


@pytest.fixture
def logger(state):
    return DecisionLogger(state.run_dir / "decision_log.json", run_id=state.run_id)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_adapter, "BASE_DIR", tmp_path)
    H.PUBLISHED.clear()
    yield
    import orchestrator
    orchestrator._ENGINE.clear()


def _entries(state):
    return json.loads((state.run_dir / "decision_log.json").read_text(encoding="utf-8"))


# --- PendingApprover + RunState ----------------------------------------------

def test_pending_is_not_an_approval_and_executor_does_not_run(state, logger):
    ran = []
    approver = PendingApprover(store=state, logger=logger)
    result = guarded_action("publish", "Publish page", {"x": 1}, ran.append, Ctx(approver, logger))
    assert result.decision == AWAITING and not result.approved and not result.executed
    assert ran == []
    assert state.status == "awaiting_approval"
    assert state.pending.kind == "publish" and state.pending.status == "pending"
    assert all(e["approved_by"] is None for e in _entries(state))


def test_pending_then_approve_is_recorded_with_founder(state, logger):
    ran = []
    approver = PendingApprover(store=state, logger=logger)
    ctx = Ctx(approver, logger)
    guarded_action("publish", "Publish page", {"x": 1}, ran.append, ctx)
    item = approver.decide(state.pending.id, True)
    assert item.status == "approved" and item.approved_by == FOUNDER
    result = guarded_action("publish", "Publish page", {"x": 1}, ran.append, ctx)
    assert result.approved and result.executed and ran == [{"x": 1}]
    entries = _entries(state)
    decision = [e for e in entries if e["action"] == "founder_decision"]
    assert decision and decision[0]["approved_by"] == "founder" and decision[0]["decision"] == "approved"
    assert any(e["action"] == "execute_publish" and e["approved_by"] == "founder" for e in entries)
    assert state.pending is None   # the decision answered exactly one question
    assert load(state.run_id, state.runs_dir).decisions[0]["approved_by"] == "founder"


def test_restart_from_state_json_shows_the_same_pending_item(state, logger):
    approver = PendingApprover(store=state, logger=logger)
    approver.set_context({"gate": "evidence_threshold", "target": "signups >= 100", "actual": "12"})
    review = approver.review_gate("Gate after 2.1: STOP", "stop", list(approval.GATE_OUTCOMES))
    assert review.awaiting and review.approver is None
    before = state.pending.to_dict()

    reloaded = load(state.run_id, state.runs_dir)          # a fresh process reads the file
    assert reloaded.status == "awaiting_approval"
    assert reloaded.pending.to_dict() == before
    assert reloaded.pending.details["target"] == "signups >= 100"
    # The decision can be made against the reloaded state.
    item = PendingApprover(store=reloaded, logger=logger).decide(before["id"], True)
    assert item.founder_outcome == "stop" and item.approved_by == FOUNDER


def test_reject_never_runs_the_executor(state, logger):
    ran = []
    approver = PendingApprover(store=state, logger=logger)
    ctx = Ctx(approver, logger)
    guarded_action("send", "Send email", {}, ran.append, ctx)
    item = approver.decide(state.pending.id, False, note="not now")
    assert item.status == "rejected" and item.approved_by is None
    result = guarded_action("send", "Send email", {}, ran.append, ctx)
    assert result.decision == "rejected" and not result.executed and ran == []
    entry = [e for e in _entries(state) if e["action"] == "founder_decision"][0]
    assert entry["approved_by"] is None and entry["decision"] == "rejected"


def test_override_requires_a_note_and_logs_both_outcomes(state, logger):
    approver = PendingApprover(store=state, logger=logger)
    approver.review_gate("Gate: STOP", "stop", list(approval.GATE_OUTCOMES))
    pid = state.pending.id
    with pytest.raises(ApprovalError):
        approver.decide(pid, True, founder_outcome="go")
    with pytest.raises(ApprovalError):
        approver.decide(pid, True, founder_outcome="go", note="   ")
    assert state.pending.status == "pending"     # nothing was recorded
    item = approver.decide(pid, True, founder_outcome="go", note="I have 40 paying pre-orders offline")
    assert item.status == "overridden" and item.founder_outcome == "go"
    entry = [e for e in _entries(state) if e["action"] == "founder_decision"][0]
    assert entry["system_outcome"] == "stop" and entry["founder_outcome"] == "go"
    assert entry["override"] is True and entry["approved_by"] == "founder"
    review = approver.review_gate("Gate: STOP", "stop", list(approval.GATE_OUTCOMES))
    assert review.founder_outcome == "go" and review.approver == FOUNDER


def test_bad_decisions_are_refused(state, logger):
    approver = PendingApprover(store=state, logger=logger)
    approver.request("publish", "Publish", {})
    pid = state.pending.id
    with pytest.raises(ApprovalError):
        approver.decide("apr-999-publish", True)
    with pytest.raises(ApprovalError):
        approver.decide(pid, True, founder_outcome="go")     # outcomes are for gates only
    with pytest.raises(ApprovalError):
        approver.decide(pid, "yes")                          # only a real True/False counts
    approver.decide(pid, True)
    with pytest.raises(ApprovalError):
        approver.decide(pid, False)                          # no second decision


def test_state_is_saved_atomically_and_listed(state):
    state.add_step({"id": "1.1", "phase": 1, "name": "x", "file": "phase1_step1_x.md"}, tokens=10)
    data = json.loads(state.path.read_text(encoding="utf-8"))
    assert data["tokens"] == 10 and data["steps"][0]["id"] == "1.1"
    assert not [p for p in state.run_dir.iterdir() if p.name.endswith(".tmp")]
    assert list_runs(state.runs_dir)[0]["run_id"] == state.run_id
    with pytest.raises(RunStateError):
        load("../escape", state.runs_dir)
    with pytest.raises(RunStateError):
        load("missing-run", state.runs_dir)


def test_no_auto_approve_path_in_app_code():
    import inspect
    import run_state
    # Approvals are built in exactly two places: the CLI "y/yes" answer and a recorded founder decision.
    assert inspect.getsource(approval).count("ApprovalResult(True") == 2
    for module in (run_state, engine_adapter):
        src = inspect.getsource(module)
        assert "ApprovalResult(True" not in src and "GateReview(" not in src


# --- engine adapter on top of the existing orchestrator ----------------------

def _start(metric=12, max_phase=2):
    rid = engine_adapter.start_run(H.brief(), {"mock_signups": metric, "max_phase": max_phase},
                                   steps=H.test_steps())
    return rid, engine_adapter.step_until_pause(rid, wait_seconds=20)


def test_adapter_pause_approve_continue_then_stop():
    rid, state = _start(metric=12)
    assert state.status == "awaiting_approval" and state.pending.is_gate
    assert state.pending.recommendation == "go" and state.pending.details["gate"] == "stub_passthrough"
    state = engine_adapter.submit_decision(rid, state.pending.id, True, wait_seconds=20)
    assert state.pending.recommendation == "stop"                 # 12 of 100 MOCKED sign-ups
    assert "12" in state.pending.details["actual"]
    state = engine_adapter.submit_decision(rid, state.pending.id, True, wait_seconds=20)
    assert state.status == "stopped" and state.final_outcome == "stop"
    assert H.PUBLISHED == []
    founder = [e for e in _entries(state) if e["action"] == "founder_decision"]
    assert len(founder) == 2 and all(e["approved_by"] == "founder" for e in founder)
    saved = load(rid, engine_adapter.runs_dir())
    assert saved.status == "stopped" and [g["outcome"] for g in saved.gates] == ["go", "stop"]


def test_adapter_reject_at_phase1_gate_halts_the_run():
    rid, state = _start()
    state = engine_adapter.submit_decision(rid, state.pending.id, False, wait_seconds=20)
    assert state.status == "stopped" and state.final_outcome == "stop"
    assert [s["id"] for s in state.steps] == ["1.1"]               # phase 2 never ran
    entry = [e for e in _entries(state) if e["action"] == "founder_decision"][0]
    assert entry["approved_by"] is None and entry["decision"] == "rejected"


def test_adapter_override_stop_to_go_needs_note_and_reaches_publish_inbox():
    rid, state = _start(metric=12)
    state = engine_adapter.submit_decision(rid, state.pending.id, True, wait_seconds=20)
    with pytest.raises(ApprovalError):
        engine_adapter.submit_decision(rid, state.pending.id, True, founder_outcome="go")
    state = engine_adapter.submit_decision(rid, state.pending.id, True, founder_outcome="go",
                                           note="testing the override path", wait_seconds=20)
    assert state.pending.kind == "publish" and state.status == "awaiting_approval"
    assert H.PUBLISHED == []                                       # still waiting: nothing ran
    gate_log = [e for e in _entries(state) if e["action"] == "founder_override"]
    assert gate_log and gate_log[0]["system_outcome"] == "stop" and gate_log[0]["founder_outcome"] == "go"
    state = engine_adapter.submit_decision(rid, state.pending.id, False, wait_seconds=20)
    assert H.PUBLISHED == [] and state.status == "stopped"
    assert not any(e["action"] == "execute_publish" for e in _entries(state))


def test_adapter_publish_approved_runs_the_mocked_executor_once():
    rid, state = _start(metric=150)
    state = engine_adapter.submit_decision(rid, state.pending.id, True, wait_seconds=20)   # 1.1 GO
    state = engine_adapter.submit_decision(rid, state.pending.id, True, wait_seconds=20)   # 2.1 GO
    assert state.pending.kind == "publish"
    state = engine_adapter.submit_decision(rid, state.pending.id, True, wait_seconds=20)
    assert len(H.PUBLISHED) == 1 and state.status == "finished"


def test_restart_from_state_json_shows_the_same_pending_item_and_continues():
    import orchestrator
    rid, state = _start(metric=12)
    pending = state.pending.to_dict()
    assert load(rid, engine_adapter.runs_dir()).cursor["stage"] == "gate"
    orchestrator._ENGINE.clear()                                   # the app process restarted
    reloaded = engine_adapter.step_until_pause(rid, steps=H.test_steps())
    assert reloaded.pending.to_dict() == pending and reloaded.status == "awaiting_approval"
    after = engine_adapter.submit_decision(rid, pending["id"], True, steps=H.test_steps())
    assert after.decisions[-1]["approved_by"] == "founder"
    assert after.pending.recommendation == "stop" and after.status == "awaiting_approval"   # the run moved on
    final = engine_adapter.submit_decision(rid, after.pending.id, True, steps=H.test_steps())
    assert final.status == "stopped" and final.final_outcome == "stop"
    assert [s["id"] for s in final.steps] == ["1.1", "2.1"]                                 # no step ran twice
    actions = [e["action"] for e in _entries(final)]
    assert actions.count("start_run") == 1 and actions.count("pause_for_founder") == 2
    assert "resume_after_founder_decision" in actions


def test_cli_path_never_pauses_and_has_no_state_file(tmp_path):
    import orchestrator
    from approval import DenyAllApprover
    summary = orchestrator.run_pipeline(H.brief(), steps=H.test_steps(), approver=DenyAllApprover(),
                                        base_dir=tmp_path, mock_signups=150, phases=[1, 2])
    assert summary.status != "awaiting_approval"
    assert not (summary.output_dir / "state.json").exists()
    assert H.PUBLISHED == []                                                                # denied by default


def test_ui_options_reach_run_pipeline():
    import orchestrator
    kw = orchestrator.engine_options({"fast": True, "max_phase": 2, "mock_signups": 12, "mock_visitors": 400,
                                      "waitlist_rows": None, "signups": None})
    assert kw == {"fast": True, "phases": [1, 2], "mock_signups": 12, "mock_visitors": 400}
    kw = orchestrator.engine_options({"max_phase": 1, "waitlist_upload": "uploaded", "visitors": 500})
    assert kw == {"fast": False, "phases": [1], "visitors": 500}     # the upload goes in as a file, not a count
    import inspect
    params = inspect.signature(orchestrator.run_pipeline).parameters
    assert all(k in params for k in ("fast", "phases", "signups", "visitors", "mock_signups", "mock_visitors"))


# --- UI waitlist upload: the CLI's checks, counts only, no silent fallback ------------------

def _approve_all(rid, **engine):
    state = engine_adapter.step_until_pause(rid, **engine)
    for _ in range(25):
        if state.pending is None or state.pending.decided:
            break
        state = engine_adapter.submit_decision(rid, state.pending.id, True, **engine)
    return state


def _decent_run(options):
    import copy
    from brief import load_brief
    from llm import FakeLLM
    from tests.test_validate_m3 import BRIEFS, REPLIES, fetch, search
    rid = engine_adapter.start_run(load_brief(BRIEFS / "decent_idea.json"), {"fast": True, "max_phase": 2, **options},
                                   llm_client=FakeLLM(copy.deepcopy(REPLIES)), search_fn=search, fetch_fn=fetch)
    return rid, _approve_all(rid)


def test_upload_counts_with_the_cli_checks():
    import orchestrator
    data = ("email" + chr(10) + ("same.person@example.com" + chr(10)) * 100).encode("utf-8")
    counts = orchestrator.count_waitlist_bytes(data)
    assert counts["counted"] == 1 and counts["duplicates"] == 99 and counts["rows"] == 100


def test_upload_of_one_email_100_times_counts_1_and_no_email_is_kept(tmp_path):
    import glob
    import tempfile
    data = ("email" + chr(10) + ("same.person@example.com" + chr(10)) * 100).encode("utf-8")
    rid, state = _decent_run({"waitlist_upload": "uploaded", "waitlist_bytes": data, "visitors": 1000,
                              "signups": 500})                     # typed sign-ups are NOT used instead
    assert state.status == "stopped" and state.final_outcome == "stop"
    counted = [e for e in _entries(state) if e["action"] == "count_waitlist"]
    assert counted and counted[-1]["counted"] == 1 and counted[-1]["duplicates"] == 99
    validate = [g for g in state.gates if g["gate"] == "validate"]
    assert validate[-1]["system_outcome"] == "stop" and "1 sign-ups" in validate[-1]["actual"]
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert "same.person@example.com" not in path.read_text(encoding="utf-8", errors="replace"), path
    assert "waitlist_bytes" not in json.dumps(state.to_dict())
    assert not glob.glob(str(Path(tempfile.gettempdir()) / "waitlist_upload_*.csv"))   # temp copy deleted


def test_unreadable_upload_stops_with_the_unreadable_reason():
    rid, state = _decent_run({"waitlist_upload": "unreadable", "signups": 500, "visitors": 1000})
    assert state.status == "stopped" and state.final_outcome == "stop"
    validate = [g for g in state.gates if g["gate"] == "validate"]
    assert validate[-1]["system_outcome"] == "stop"
    assert validate[-1]["reasons"][0].startswith("The --waitlist export could not be read")
    entry = [e for e in _entries(state) if e["action"] == "waitlist_unreadable"][-1]
    assert entry["ignored_evidence"] == ["--signups"]
