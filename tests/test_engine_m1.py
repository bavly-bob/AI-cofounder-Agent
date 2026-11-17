"""M1 engine tests. No Ollama, no network. Everything is written under pytest tmp dirs."""
import json
import re
from pathlib import Path

import pytest

import approval
import orchestrator
from approval import (ApprovalRequired, ApprovalResult, Approver, CLIApprover, DenyAllApprover,
                      GateReview, guarded_action, mock_publish, requires_founder_approval)
from brief import BriefError, FounderBrief, load_brief, parse_brief_text
from decision_log import REQUIRED_KEYS, DecisionLogger, LogValidationError
from gates import (EvidenceThresholdGate, FounderApprovalGate, Outcome, SourcedClaimsGate,
                   StubPassThroughGate, TestsPassGate, render_decision_md)
from orchestrator import MAX_REPEATS, run_pipeline
from steps import (OUTPUT_NAME_RE, Claim, DummyDiscoverStep, DummyValidateStep, RunContext,
                   StepResult, register_step, registered_steps, render_claim)

ROOT = Path(__file__).resolve().parent.parent
BRIEFS = ROOT / "examples" / "briefs"
NAME_RE = re.compile(r"^phase\d+_(step\d+|gate)_[a-z0-9_]+\.(md|json)$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class ScriptedApprover(Approver):
    """Test-only approver: answers from a script. Lives under tests/ and nowhere else."""

    def __init__(self, approve=False, gate_answers=None):
        self.approve, self.gate_answers, self.requests = approve, dict(gate_answers or {}), []

    def request(self, kind, summary, payload):
        self.requests.append(kind)
        return ApprovalResult(self.approve, "founder", "scripted test answer")

    def review_gate(self, summary, system_outcome, options):
        answer = self.gate_answers.get(system_outcome)
        return GateReview(answer, "founder" if answer else None, "scripted test answer")


def run(tmp_path, brief_name="weak_idea.json", **kwargs):
    return run_pipeline(load_brief(BRIEFS / brief_name), base_dir=tmp_path, **kwargs)


def log_of(summary):
    return json.loads(summary.log_path.read_text(encoding="utf-8"))


def files_of(summary):
    return sorted(p.name for p in summary.output_dir.iterdir() if p.name != "decision_log.json")


def ctx_for(tmp_path, approver=None):
    logger = DecisionLogger(tmp_path / "decision_log.json", run_id="test-run")
    return RunContext(FounderBrief("an idea"), "test-run", logger, approver or DenyAllApprover(), tmp_path)


def metric_result(value=None, source="MOCKED: test"):
    data = {"metrics": {}} if value is None else {"metrics": {"signups": {"value": value, "source": source}}}
    return StepResult(2, 1, "validation", "tester", "body", [], data, None, 0, None, "STUBBED")


# --- pipeline outcomes -------------------------------------------------------

def test_metric_12_against_100_stops_and_writes_stop_memo(tmp_path):
    summary = run(tmp_path, metric=12)
    assert summary.status == "stopped" and summary.final_outcome == "stop"
    assert files_of(summary) == [
        "phase1_gate_stub_passthrough.md", "phase1_step1_opportunities.md",
        "phase2_gate_evidence_threshold.md", "phase2_gate_stop_memo.md", "phase2_step1_validation.md",
    ]
    memo = (summary.output_dir / "phase2_gate_stop_memo.md").read_text(encoding="utf-8")
    assert "STOP" in memo and "## Why" in memo and "## What evidence would change this decision" in memo
    assert summary.output_dir.parent == tmp_path / "runs"
    # the founder-gated publish was never reached
    assert not [e for e in log_of(summary) if e["action"] == "approval_request"]


def test_gate_target_is_logged_before_the_step_runs(tmp_path):
    actions = [(e["action"], e.get("gate")) for e in log_of(run(tmp_path, metric=12))]
    assert actions.index(("set_gate_target", "evidence_threshold")) < actions.index(("run_validation", None))
    target = next(e for e in log_of(run(tmp_path, metric=12))
                  if e["action"] == "set_gate_target" and e["gate"] == "evidence_threshold")
    assert target["target"] == 100 and target["comparator"] == ">="


def test_missing_evidence_stops(tmp_path):
    summary = run(tmp_path)  # no --metric and no mock_metric in the notes
    assert summary.final_outcome == "stop"
    gate = (summary.output_dir / "phase2_gate_evidence_threshold.md").read_text(encoding="utf-8")
    assert "Missing evidence" in gate and "STOP" in gate
    assert (summary.output_dir / "phase2_gate_stop_memo.md").is_file()


@pytest.mark.parametrize("data", [{}, {"metrics": {}}, {"metrics": {"signups": None}}, {"signups": 500},
                                  {"metrics": {"signups": {"value": None, "source": "x"}}},
                                  {"metrics": {"signups": {"value": "500", "source": "x"}}},
                                  {"metrics": {"signups": {"value": float("nan"), "source": "x"}}},
                                  {"metrics": {"signups": {"value": 500}}},
                                  {"metrics": {"signups": {"value": 500, "source": "  "}}}])
def test_missing_or_unsourced_evidence_never_passes(tmp_path, data):
    result = StepResult(2, 1, "validation", "tester", "body", [], data)
    decision = EvidenceThresholdGate("signups", 100).evaluate(result, ctx_for(tmp_path), attempts_remaining=2)
    assert decision.outcome == Outcome.STOP


def test_threshold_gate_rules(tmp_path):
    gate, ctx = EvidenceThresholdGate("signups", 100, ">=", "7 days"), ctx_for(tmp_path)
    assert gate.evaluate(metric_result(100), ctx, 2).outcome == Outcome.GO
    assert gate.evaluate(metric_result(120), ctx, 0).outcome == Outcome.GO
    assert gate.evaluate(metric_result(99), ctx, 1).outcome == Outcome.REPEAT
    assert gate.evaluate(metric_result(50), ctx, 1).outcome == Outcome.REPEAT
    assert gate.evaluate(metric_result(50), ctx, 0).outcome == Outcome.PIVOT
    assert gate.evaluate(metric_result(49.9), ctx, 2).outcome == Outcome.STOP
    assert gate.evaluate(metric_result(12), ctx, 2).outcome == Outcome.STOP
    lower = EvidenceThresholdGate("signups", 10, "<=")
    assert lower.evaluate(metric_result(8), ctx, 0).outcome == Outcome.GO
    assert lower.evaluate(metric_result(15), ctx, 1).outcome == Outcome.REPEAT
    assert lower.evaluate(metric_result(25), ctx, 1).outcome == Outcome.STOP


def test_between_half_and_target_repeats_then_pivots(tmp_path):
    summary = run(tmp_path, metric=60)
    outcomes = [g["outcome"] for g in summary.gates if g["gate"] == "evidence_threshold"]
    assert outcomes == ["repeat"] * MAX_REPEATS + ["pivot"]
    assert len([s for s in summary.steps if s["id"] == "2.1"]) == MAX_REPEATS + 1
    assert summary.final_outcome == "pivot" and summary.status == "stopped"
    names = files_of(summary)
    assert "phase2_step1_validation_attempt3.md" in names and "phase2_gate_stop_memo.md" in names
    assert not [e for e in log_of(summary) if e["action"] == "approval_request"]


def test_pivot_goes_back_to_a_named_earlier_step_once(tmp_path):
    class PivotingValidate(DummyValidateStep):
        pivot_to = (1, 1)

    steps = [DummyDiscoverStep(), PivotingValidate()]
    summary = run_pipeline(load_brief(BRIEFS / "weak_idea.json"), steps=steps, base_dir=tmp_path, metric=60)
    log = log_of(summary)
    assert len([e for e in log if e["action"] == "pivot"]) == 1
    assert len([s for s in summary.steps if s["id"] == "1.1"]) == 2
    assert summary.final_outcome == "pivot"


def test_excluded_crypto_brief_stops_before_any_step(tmp_path):
    ran = []

    class SpyStep(DummyDiscoverStep):
        def run(self, ctx):
            ran.append(1)
            return super().run(ctx)

    summary = run_pipeline(load_brief(BRIEFS / "excluded_crypto.json"), steps=[SpyStep()], base_dir=tmp_path)
    assert ran == [] and summary.steps == [] and summary.gates == []
    assert summary.final_outcome == "stop"
    assert files_of(summary) == ["phase0_gate_stop_memo.md"]
    refusals = [e for e in log_of(summary) if e["agent"] == "guardrail" and e["action"] == "refuse_brief"]
    assert len(refusals) == 1 and refusals[0]["category"] == "excluded_product"
    assert not [e for e in log_of(summary) if e["action"].startswith("run_")]


def test_decent_brief_passes_the_brief_check(tmp_path):
    summary = run(tmp_path, "decent_idea.json", metric=120)
    assert [s["id"] for s in summary.steps] == ["1.1", "2.1"]


def test_blocked_output_is_logged_and_never_saved(tmp_path, monkeypatch):
    class Loud(DummyDiscoverStep):
        def run(self, ctx):
            result = super().run(ctx)
            result.content_md = "FORBIDDEN-MARKER this is refused"
            return result

    def fake_check_output(text):
        blocked = "FORBIDDEN-MARKER" in text
        return orchestrator.guardrails.GuardrailResult(not blocked, "income_promise" if blocked else None,
                                                       "refused in test", "an honest alternative")

    monkeypatch.setattr(orchestrator.guardrails, "check_output", fake_check_output)
    summary = run_pipeline(load_brief(BRIEFS / "weak_idea.json"), steps=[Loud()], base_dir=tmp_path)
    refusals = [e for e in log_of(summary) if e["action"] == "refuse_output"]
    assert len(refusals) == MAX_REPEATS + 1 and refusals[0]["category"] == "income_promise"
    assert summary.final_outcome == "stop" and files_of(summary) == ["phase1_gate_stop_memo.md"]
    for path in summary.output_dir.iterdir():
        assert "FORBIDDEN-MARKER" not in path.read_text(encoding="utf-8")


def test_a_crashing_step_or_guardrail_does_not_crash_the_run(tmp_path, monkeypatch):
    class Broken(DummyDiscoverStep):
        def run(self, ctx):
            raise RuntimeError("boom")

    summary = run_pipeline(load_brief(BRIEFS / "weak_idea.json"), steps=[Broken()], base_dir=tmp_path / "a")
    assert summary.status == "error" and "boom" in summary.stop_reasons[0]

    def broken_check(text):
        raise RuntimeError("guardrail down")

    monkeypatch.setattr(orchestrator.guardrails, "check_brief", broken_check)
    summary = run(tmp_path / "b", metric=120)
    assert summary.final_outcome == "stop" and summary.steps == []  # fails closed


# --- approval ----------------------------------------------------------------

def test_deny_all_never_calls_the_executor_and_logs_the_denial(tmp_path):
    calls, ctx = [], ctx_for(tmp_path, DenyAllApprover())
    outcome = guarded_action("publish", "publish a page", {"a": 1}, lambda p: calls.append(p), ctx)
    assert calls == [] and not outcome.approved and not outcome.executed
    actions = [e["action"] for e in ctx.logger.entries()]
    assert actions == ["approval_request", "approval_denied"]
    assert all(e["approved_by"] is None for e in ctx.logger.entries())


def test_pipeline_default_approver_denies_publish(tmp_path):
    summary = run(tmp_path, "decent_idea.json", metric=120)  # default approver: DenyAllApprover
    log = log_of(summary)
    assert [e["action"] for e in log if e["agent"] == "approval"] == ["approval_request", "approval_denied"]
    assert not [e for e in log if e["action"].startswith("execute_")]
    assert not [e for e in log if e["approved_by"] == "founder"]
    assert summary.final_outcome == "stop"
    gate = (summary.output_dir / "phase2_gate_founder_approval.md").read_text(encoding="utf-8")
    assert "NOT approved" in gate and "Nothing was published" in gate


def test_founder_approval_runs_the_mocked_executor(tmp_path):
    approver = ScriptedApprover(approve=True)
    summary = run(tmp_path, "decent_idea.json", metric=120, approver=approver)
    assert summary.status == "completed" and summary.final_outcome == "go" and approver.requests == ["publish"]
    executed = [e for e in log_of(summary) if e["action"] == "execute_publish"]
    assert len(executed) == 1 and executed[0]["approved_by"] == "founder" and executed[0]["status"] == "MOCKED"


def test_calling_a_gated_function_directly_raises(tmp_path):
    with pytest.raises(ApprovalRequired):
        mock_publish({"page": "x"})
    with pytest.raises(ApprovalRequired):
        approval.mock_send({"to": "x"})

    @requires_founder_approval("spend")
    def spend(payload):
        return "spent"

    with pytest.raises(ApprovalRequired):
        spend({})
    ctx = ctx_for(tmp_path, ScriptedApprover(approve=True))
    assert guarded_action("spend", "s", {}, spend, ctx).result == "spent"
    with pytest.raises(ApprovalRequired):  # the approval does not outlive the guarded call
        spend({})
    # an approval for one kind does not unlock another kind
    wrong = guarded_action("send", "s", {}, spend, ctx)
    assert wrong.approved and not wrong.executed and "founder approval" in wrong.error


def test_guarded_action_rejects_unknown_kinds_and_odd_answers(tmp_path):
    with pytest.raises(ValueError):
        guarded_action("refund", "s", {}, lambda p: p, ctx_for(tmp_path))

    class Sloppy(Approver):
        def request(self, kind, summary, payload):
            return ApprovalResult("yes", "robot", "")   # not True, not the founder

    class Crashing(Approver):
        def request(self, kind, summary, payload):
            raise RuntimeError("ui died")

    for approver in (Sloppy(), Crashing(), None):
        calls, ctx = [], ctx_for(tmp_path, approver)
        ctx.approver = approver
        assert not guarded_action("sign", "s", {}, lambda p: calls.append(p), ctx).approved
        assert calls == []


def test_cli_approver_default_is_no(monkeypatch, capsys):
    def answers(value):
        def fake_input(prompt=""):
            if isinstance(value, BaseException):
                raise value
            return value
        return fake_input

    for value, expected in [("y", True), ("YES", True), ("", False), ("n", False), ("maybe", False),
                            ("yy", False), (EOFError(), False), (KeyboardInterrupt(), False)]:
        monkeypatch.setattr("builtins.input", answers(value))
        assert CLIApprover().request("publish", "s", {"k": "v"}).approved is expected, value
    assert "Approve? [y/N]" in Path(approval.__file__).read_text(encoding="utf-8")


def test_no_auto_approve_path_in_app_code():
    pattern = re.compile(r"auto[_-]?approv|--yes\b|assume[_-]?yes|APPROVE_ALL|ScriptedApprover|AllowAll", re.I)
    paths = [p for p in ROOT.glob("*.py") if p.name != "guardrails.py"]  # its rules may quote such phrases
    for path in paths + list((ROOT / "agents").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert not pattern.search(text), path.name
    for module in (approval, orchestrator):  # approval never depends on an environment variable
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "os.environ" not in source and "getenv" not in source


# --- founder confirm / override ----------------------------------------------

def test_system_never_upgrades_a_stop_but_the_founder_can_override(tmp_path):
    untouched = run(tmp_path / "a", metric=12)
    assert untouched.final_outcome == "stop"
    assert not [e for e in log_of(untouched) if e["approved_by"] == "founder"]

    approver = ScriptedApprover(approve=False, gate_answers={"stop": "go"})
    summary = run(tmp_path / "b", metric=12, approver=approver)
    overrides = [e for e in log_of(summary) if e["action"] == "founder_override"]
    assert len(overrides) == 1
    assert overrides[0]["system_outcome"] == "stop" and overrides[0]["founder_outcome"] == "go"
    assert overrides[0]["approved_by"] == "founder"
    assert approver.requests == ["publish"]  # the run went on to the publish approval, which was denied
    gate = (summary.output_dir / "phase2_gate_evidence_threshold.md").read_text(encoding="utf-8")
    assert "OVERRIDE by the founder: system outcome STOP, founder outcome GO" in gate
    assert "Target NOT met" in gate  # the failed result is still stated


def test_founder_confirmation_is_logged(tmp_path):
    summary = run(tmp_path, metric=12, approver=ScriptedApprover(gate_answers={"go": "go", "stop": "stop"}))
    confirms = [e for e in log_of(summary) if e["action"] == "founder_confirm"]
    assert [c["outcome"] for c in confirms] == ["go", "stop"]
    assert all(c["approved_by"] == "founder" for c in confirms)


def test_a_non_founder_gate_review_changes_nothing(tmp_path):
    class Impostor(Approver):
        def request(self, kind, summary, payload):
            return ApprovalResult(False, None, "")

        def review_gate(self, summary, system_outcome, options):
            return GateReview("go", "system", "")

    summary = run(tmp_path, metric=12, approver=Impostor())
    assert summary.final_outcome == "stop"


# --- other gates -------------------------------------------------------------

def test_sourced_claims_gate(tmp_path):
    ctx = ctx_for(tmp_path)

    def result(claims):
        return StepResult(1, 1, "x", "tester", "body", claims)

    sourced, assumed = Claim("a", "https://example.org/a"), Claim("b", None)
    gate = SourcedClaimsGate(1.0)
    assert gate.evaluate(result([sourced, sourced]), ctx, 0).outcome == Outcome.GO
    assert gate.evaluate(result([]), ctx, 2).outcome == Outcome.STOP
    assert gate.evaluate(result([sourced, assumed]), ctx, 1).outcome == Outcome.REPEAT
    assert gate.evaluate(result([sourced, assumed]), ctx, 0).outcome == Outcome.PIVOT
    assert gate.evaluate(result([assumed, assumed, Claim("c", "not a url")]), ctx, 2).outcome == Outcome.STOP


def test_tests_pass_gate(tmp_path):
    ctx, result = ctx_for(tmp_path), metric_result(1)
    assert TestsPassGate(lambda: True).evaluate(result, ctx, 0).outcome == Outcome.GO
    assert TestsPassGate(lambda: (True, "3 passed")).evaluate(result, ctx, 0).outcome == Outcome.GO
    assert TestsPassGate(lambda: False).evaluate(result, ctx, 1).outcome == Outcome.REPEAT
    assert TestsPassGate(lambda: (False, "1 failed")).evaluate(result, ctx, 0).outcome == Outcome.STOP
    assert TestsPassGate(lambda: 1 / 0).evaluate(result, ctx, 2).outcome == Outcome.STOP
    assert TestsPassGate(lambda: "yes").evaluate(result, ctx, 0).outcome == Outcome.STOP


def test_founder_approval_gate_delegates_to_approval(tmp_path):
    calls = []

    @requires_founder_approval("send")
    def send(payload):
        calls.append(payload)
        return {"status": "MOCKED"}

    gate = FounderApprovalGate("send", "send one email", send)
    denied = gate.evaluate(metric_result(1), ctx_for(tmp_path / "a"), 0)
    assert denied.outcome == Outcome.STOP and calls == []
    granted = gate.evaluate(metric_result(1), ctx_for(tmp_path / "b", ScriptedApprover(approve=True)), 0)
    assert granted.outcome == Outcome.GO and len(calls) == 1 and granted.recommended_by == "founder"


def test_gate_markdown_states_failure_plainly(tmp_path):
    gate, ctx = EvidenceThresholdGate("signups", 100, ">=", "7 days"), ctx_for(tmp_path)
    for value, word in [(12, "STOP"), (60, "REPEAT")]:
        text = render_decision_md(gate.evaluate(metric_result(value), ctx, 1), "r", 2, 1)
        assert f"**Outcome: {word}" in text and "NOT met" in text
        assert "Target (set before the step ran): signups >= 100" in text and f"Actual: {value}" in text
        assert "## Founder options" in text and "## Reasons" in text
        assert not re.search(r"promising|great|success|congrat|encouraging|good news|almost|nearly", text, re.I)


# --- decision log ------------------------------------------------------------

def test_every_log_entry_has_the_required_keys(tmp_path):
    entries = []
    for i, (name, metric, approver) in enumerate([
        ("weak_idea.json", 12, None), ("weak_idea.json", 60, None), ("weak_idea.json", None, None),
        ("excluded_crypto.json", None, None), ("decent_idea.json", 120, None),
        ("decent_idea.json", 120, ScriptedApprover(approve=True, gate_answers={"go": "go"})),
    ]):
        entries += log_of(run(tmp_path / str(i), name, metric=metric, approver=approver))
    assert len(entries) > 30
    for entry in entries:
        for key in ("timestamp", "agent", "action", "sources", "model", "cost", "approved_by"):
            assert key in entry, (key, entry)
        assert TS_RE.match(entry["timestamp"])
        assert isinstance(entry["agent"], str) and isinstance(entry["action"], str)
        assert isinstance(entry["sources"], list)
        assert all(s.startswith(("http://", "https://")) for s in entry["sources"])
        assert entry["model"] is None or isinstance(entry["model"], str)
        assert isinstance(entry["cost"], (int, float)) and not isinstance(entry["cost"], bool)
        assert entry["approved_by"] in ("founder", None)
        assert entry["run_id"]
    assert set(REQUIRED_KEYS) == {"timestamp", "agent", "action", "sources", "model", "cost", "approved_by"}
    kinds = {e["action"] for e in entries}
    assert {"start_run", "end_run", "set_gate_target", "run_opportunities", "run_validation",
            "evidence_threshold", "approval_request", "approval_denied", "approval_granted",
            "execute_publish", "refuse_brief", "write_stop_memo", "founder_confirm"} <= kinds
    step_entry = next(e for e in entries if e["action"] == "run_opportunities")
    assert step_entry["sources"] == []  # the STUBBED step retrieved nothing, so it logs no source
    assert all(e["sources"] == [] for e in entries)
    assert step_entry["status"] == "STUBBED" and step_entry["phase"] == 1 and step_entry["step"] == 1


def test_log_validates_on_write_and_stays_intact(tmp_path):
    logger = DecisionLogger(tmp_path / "log" / "decision_log.json")
    logger.log("a", "ok", sources=["https://example.org/x"], model="m", cost=0.5, outcome="go")
    bad = [dict(sources=["ftp://example.org"]), dict(sources=["not a url"]), dict(sources="https://example.org"),
           dict(sources=[None]), dict(cost="free"), dict(cost=True), dict(cost=float("nan")), dict(cost=-1),
           dict(approved_by="system"), dict(approved_by=True), dict(model=3), dict(timestamp="yesterday")]
    for kwargs in bad:
        with pytest.raises(LogValidationError):
            logger.log("a", "bad", **kwargs)
    with pytest.raises(LogValidationError):
        logger.log("", "bad")
    entries = logger.entries()
    assert len(entries) == 1 and entries[0]["outcome"] == "go" and entries[0]["approved_by"] is None
    assert [p.name for p in (tmp_path / "log").iterdir()] == ["decision_log.json"]  # no temp files left
    assert json.loads((tmp_path / "log" / "decision_log.json").read_text(encoding="utf-8")) == entries


def test_log_is_append_only_across_loggers(tmp_path):
    path = tmp_path / "decision_log.json"
    DecisionLogger(path).log("a", "first")
    DecisionLogger(path).log("a", "second")
    assert [e["action"] for e in DecisionLogger(path).entries()] == ["first", "second"]


# --- output files ------------------------------------------------------------

def test_output_names_headers_and_labels(tmp_path):
    runs = [run(tmp_path / "a", metric=12), run(tmp_path / "b", metric=60),
            run(tmp_path / "c", "excluded_crypto.json"), run(tmp_path / "d", "decent_idea.json", metric=120)]
    assert OUTPUT_NAME_RE.pattern == NAME_RE.pattern
    for summary in runs:
        names = files_of(summary)
        assert names and sorted(summary.files) == names
        for name in names:
            assert NAME_RE.match(name), name
            first = (summary.output_dir / name).read_text(encoding="utf-8").splitlines()[0]
            assert f"run_id: {summary.run_id}" in first and "model:" in first and "status:" in first
    out = runs[0].output_dir
    discover = (out / "phase1_step1_opportunities.md").read_text(encoding="utf-8")
    assert "status: STUBBED" in discover.splitlines()[0]
    assert "(ASSUMPTION)" in discover and "http" not in discover and "[source]" not in discover
    validate = (out / "phase2_step1_validation.md").read_text(encoding="utf-8")
    assert "status: STUBBED" in validate.splitlines()[0] and "MOCKED" in validate and "12" in validate


def test_denied_by_default_is_not_attributed_to_the_founder(tmp_path, monkeypatch):
    def closed_input(prompt=""):
        raise EOFError

    class Crashing(Approver):
        def request(self, kind, summary, payload):
            raise RuntimeError("ui died")

    monkeypatch.setattr("builtins.input", closed_input)
    for i, approver in enumerate([None, DenyAllApprover(), CLIApprover(), Crashing()]):
        summary = run(tmp_path / str(i), "decent_idea.json", metric=120, approver=approver)
        assert summary.final_outcome == "stop"
        text = (summary.output_dir / "phase2_gate_founder_approval.md").read_text(encoding="utf-8")
        assert "Recommended by: system" in text
        assert "No founder decision was recorded. The action was denied by default (approved_by: null)." in text
        assert "denied by default" in text and "Nothing was published" in text
        assert not re.search(r"founder (decided|rejected)|Recommended by: founder|approved_by: founder", text, re.I)
        assert "## Founder options" not in text and "`accept`" not in text and "`pivot`" not in text
        log = log_of(summary)
        gate = next(e for e in log if e["action"] == "founder_approval")
        assert gate["recommended_by"] == "system" and gate["approved_by"] is None
        assert gate["decision"] == "denied_by_default"
        denied = next(e for e in log if e["action"] == "approval_denied")
        assert denied["decision"] == "denied_by_default" and denied["approved_by"] is None
        assert not [e for e in log if e["approved_by"] == "founder"]
        assert summary.gates[-1]["founder_outcome"] is None
        memo = (summary.output_dir / "phase2_gate_stop_memo.md").read_text(encoding="utf-8")
        assert "Override a gate recommendation" not in memo


def test_explicit_founder_no_is_a_rejection_with_approved_by_null(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    summary = run(tmp_path, "decent_idea.json", metric=120, approver=CLIApprover())
    text = (summary.output_dir / "phase2_gate_founder_approval.md").read_text(encoding="utf-8")
    assert "Recommended by: founder" in text
    assert "The founder rejected this publish action at the prompt. Nothing was done (approved_by: null)." in text
    assert "approved_by: founder" not in text and "## Founder options" not in text
    log = log_of(summary)
    gate = next(e for e in log if e["action"] == "founder_approval")
    assert gate["decision"] == "rejected" and gate["approved_by"] is None and gate["recommended_by"] == "founder"
    denied = next(e for e in log if e["action"] == "approval_denied")
    assert denied["decision"] == "rejected" and denied["approved_by"] is None
    assert not [e for e in log if e["approved_by"] == "founder"]


def test_approved_action_gate_file(tmp_path):
    summary = run(tmp_path, "decent_idea.json", metric=120, approver=ScriptedApprover(approve=True))
    text = (summary.output_dir / "phase2_gate_founder_approval.md").read_text(encoding="utf-8")
    assert "The founder approved this publish action (approved_by: founder)." in text
    gate = next(e for e in log_of(summary) if e["action"] == "founder_approval")
    assert gate["decision"] == "approved" and gate["approved_by"] == "founder"


def test_a_denied_action_cannot_be_overridden_to_go_at_a_gate_prompt(tmp_path):
    approver = ScriptedApprover(approve=False, gate_answers={"stop": "go", "go": "go"})
    summary = run(tmp_path, "decent_idea.json", metric=120, approver=approver)
    assert summary.final_outcome == "stop"
    assert not [e for e in log_of(summary) if e["action"].startswith("execute_")]


def test_stubbed_step_gate_is_labelled_a_stub_passthrough(tmp_path):
    summary = run(tmp_path, "decent_idea.json", metric=120)
    assert not (summary.output_dir / "phase1_gate_sourced_claims.md").exists()
    text = (summary.output_dir / "phase1_gate_stub_passthrough.md").read_text(encoding="utf-8")
    assert "status: STUBBED" in text.splitlines()[0] and "status: WORKING" not in text
    assert "**Outcome: GO (STUBBED step \u2014 placeholder, not real evidence)" in text
    assert "No evidence was checked" in text and "http" not in text
    assert not re.search(r"target (was )?met", text, re.I)
    entry = next(e for e in log_of(summary) if e["action"] == "stub_passthrough")
    assert entry["basis"] == "STUBBED" and entry["sources"] == [] and entry["outcome"] == "go"
    # a real step is never waved through by this gate
    real = StepResult(1, 1, "x", "tester", "body", [], {}, None, 0, None, "WORKING")
    assert StubPassThroughGate().evaluate(real, ctx_for(tmp_path / "g"), 2).outcome == Outcome.STOP
    # and unsourced claims still fail the real sourced-claims gate
    stub = DummyDiscoverStep().run(ctx_for(tmp_path / "h"))
    assert all(c.source_url is None for c in stub.claims)
    assert SourcedClaimsGate(0.5).evaluate(stub, ctx_for(tmp_path / "i"), 0).outcome == Outcome.STOP


def test_gate_on_a_mocked_metric_is_labelled_mocked(tmp_path):
    summary = run(tmp_path / "a", "decent_idea.json", metric=120)
    text = (summary.output_dir / "phase2_gate_evidence_threshold.md").read_text(encoding="utf-8")
    assert "status: MOCKED" in text.splitlines()[0]
    assert "**Outcome: GO (MOCKED \u2014 not a real validation)" in text
    assert "The target was met.**" not in text and "only by a MOCKED value" in text
    entry = next(e for e in log_of(summary) if e["action"] == "evidence_threshold")
    assert entry["basis"] == "MOCKED"
    stopped = run(tmp_path / "b", metric=12)
    text = (stopped.output_dir / "phase2_gate_evidence_threshold.md").read_text(encoding="utf-8")
    assert "**Outcome: STOP (MOCKED \u2014 not a real validation)" in text and "Target NOT met" in text
    assert "status: MOCKED" in (stopped.output_dir / "phase2_gate_stop_memo.md").read_text(
        encoding="utf-8").splitlines()[0]
    # a real (not mocked) metric on a WORKING step keeps the plain wording
    real = StepResult(2, 1, "validation", "tester", "body", [],
                      {"metrics": {"signups": {"value": 120, "source": "https://example.org/report"}}})
    decision = EvidenceThresholdGate("signups", 100).evaluate(real, ctx_for(tmp_path / "c"), 0)
    assert decision.basis == "WORKING"
    assert "**Outcome: GO. The target was met.**" in render_decision_md(decision, "r", 2, 1)


def test_brief_text_for_guardrails_includes_every_free_text_field(tmp_path, monkeypatch):
    brief = FounderBrief("IDEA-X", skills=["SKILL-X", "SKILL-Y"], target_market="MARKET-X",
                         values=["VALUE-X"], notes="NOTES-X")
    for part in ("IDEA-X", "SKILL-X", "SKILL-Y", "MARKET-X", "VALUE-X", "NOTES-X"):
        assert part in brief.as_text()
    seen = []

    def spy(text):
        seen.append(text)
        return orchestrator.guardrails.GuardrailResult("SKILL-X" not in text, "excluded_product", "r", "alt")

    monkeypatch.setattr(orchestrator.guardrails, "check_brief", spy)
    summary = run_pipeline(brief, base_dir=tmp_path, metric=120)
    assert "VALUE-X" in seen[0] and summary.steps == [] and summary.final_outcome == "stop"
    # an excluded product named only in skills or values is caught by the real guardrails
    monkeypatch.undo()
    for field_name in ("skills", "values"):
        hidden = FounderBrief("A weekly newsletter for designers", **{field_name: ["crypto trading signals"]})
        result = run_pipeline(hidden, base_dir=tmp_path / field_name, metric=120)
        assert result.steps == [] and result.final_outcome == "stop", field_name


def test_claim_rendering():
    assert render_claim(Claim("Text", "https://example.org/a")) == "Text [source](https://example.org/a)"
    assert render_claim(Claim("Text", None)) == "Text (ASSUMPTION)"
    assert render_claim(Claim("Text", "see my notes")) == "Text (ASSUMPTION)"


def test_dummy_metric_comes_from_flag_or_notes_never_random(tmp_path):
    brief = FounderBrief("an idea", notes="early test, mock_metric=130")
    from_notes = run_pipeline(brief, base_dir=tmp_path / "a")
    assert from_notes.gates[1]["gate"] == "evidence_threshold" and from_notes.gates[1]["outcome"] == "go"
    flag_wins = run_pipeline(brief, base_dir=tmp_path / "b", metric=12)
    assert flag_wins.gates[1]["outcome"] == "stop"
    for name in ("steps.py", "orchestrator.py", "gates.py"):
        assert "random" not in (ROOT / name).read_text(encoding="utf-8").replace("random number", "")


def test_demo_mode_writes_outputs_and_root_log_of_base_dir(tmp_path):
    summary = run(tmp_path, metric=12, demo=True)
    assert summary.output_dir == tmp_path / "outputs" and summary.log_path == tmp_path / "decision_log.json"
    assert (tmp_path / "outputs" / "phase2_gate_stop_memo.md").is_file()
    assert not (tmp_path / "runs").exists()
    first = len(log_of(summary))
    assert len(log_of(run(tmp_path, metric=12, demo=True))) == first  # the demo log holds one run


def test_step_registry_accepts_new_steps(tmp_path):
    import steps as steps_module

    before = dict(steps_module._REGISTRY)
    try:
        @register_step
        class ExtraStep:
            phase, step, name, agent = 3, 1, "extra", "tester"

            def run(self, ctx):
                return StepResult(3, 1, "extra", "tester", "# extra", [], {}, None, 0, 7, "STUBBED")

        ids = [(s.phase, s.step) for s in registered_steps()]
        assert ids == [(1, 1), (2, 1), (3, 1)]
        summary = run(tmp_path, "decent_idea.json", metric=120, approver=ScriptedApprover(approve=True))
        assert [s["id"] for s in summary.steps] == ["1.1", "2.1", "3.1"] and summary.total_tokens == 7
    finally:
        steps_module._REGISTRY.clear()
        steps_module._REGISTRY.update(before)


# --- brief -------------------------------------------------------------------

def test_example_briefs_load():
    for name in ("weak_idea.json", "decent_idea.json", "excluded_crypto.json"):
        brief = load_brief(BRIEFS / name)
        assert brief.idea and isinstance(brief.skills, list) and isinstance(brief.values, list)
        assert brief.budget_usd is not None and brief.time_per_week_hours is not None and brief.target_market


def test_load_brief_errors_are_plain(tmp_path):
    with pytest.raises(BriefError):
        load_brief(tmp_path / "missing.json")
    for i, content in enumerate(["{not json", "[1, 2]", '{"skills": ["x"]}']):
        path = tmp_path / f"bad{i}.json"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(BriefError):
            load_brief(path)


def test_parse_brief_text_never_crashes():
    def boom(prompt):
        raise RuntimeError("no model")

    replies = [boom, lambda p: {"text": "", "model": "m", "cost": 0, "error": "Ollama is down"},
               lambda p: {"text": "sorry, I cannot", "model": "m", "cost": 0},
               lambda p: {"text": '{"idea": 5, "skills": {"a": 1}, "budget_usd": "lots"', "model": "m", "cost": 0},
               lambda p: "not a dict", lambda p: None, lambda p: {"text": "[1, 2]"}]
    for generate in replies:
        brief = parse_brief_text("Sell Notion templates to translators", generate=generate)
        assert brief.idea == "Sell Notion templates to translators"
        assert brief.target_market.startswith("ASSUMPTION") and "ASSUMPTION" in brief.notes
        assert brief.skills == [] and brief.budget_usd is None and brief.time_per_week_hours is None
    for odd in (None, "", 42, "x" * 20000):
        assert isinstance(parse_brief_text(odd, generate=boom), FounderBrief)


def test_parse_brief_text_uses_a_good_model_reply():
    reply = ('<think>hmm</think> Here you go: {"idea": "Notion templates", "skills": "design, writing", '
             '"budget_usd": "200 USD", "time_per_week_hours": 10, "target_market": null, "values": ["honesty"]}')
    brief = parse_brief_text("free text", generate=lambda p: {"text": reply, "model": "m", "cost": 0})
    assert brief.idea == "Notion templates" and brief.skills == ["design", "writing"]
    assert brief.budget_usd == 200 and brief.time_per_week_hours == 10
    assert brief.target_market.startswith("ASSUMPTION") and brief.values == ["honesty"]


def test_parse_brief_text_without_ollama(monkeypatch):
    import requests

    def refuse(*args, **kwargs):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr("llm.requests.post", refuse)
    assert parse_brief_text("An idea in plain words").idea == "An idea in plain words"


# --- CLI ---------------------------------------------------------------------

def test_cli_prints_a_summary_and_writes_only_under_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)

    def closed_input(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed_input)
    assert orchestrator.main(["--brief", str(BRIEFS / "weak_idea.json"), "--metric", "12"]) == 0
    out = capsys.readouterr().out
    assert "RUN SUMMARY" in out and "Final outcome : STOP" in out
    assert "Total cost    : $0.00" in out and "Total tokens  : 0" in out
    assert "evidence_threshold" in out and "phase2_gate_stop_memo.md" in out
    assert [p.name for p in tmp_path.iterdir()] == ["runs"]

    assert orchestrator.main(["--brief", str(BRIEFS / "excluded_crypto.json")]) == 0
    out = capsys.readouterr().out
    assert "(no step ran)" in out and "Final outcome : STOP" in out

    assert orchestrator.main(["--brief", str(tmp_path / "nope.json")]) == 2
    assert "Could not load the brief" in capsys.readouterr().err


def test_cli_denied_publish_is_logged(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert orchestrator.main(["--brief", str(BRIEFS / "decent_idea.json"), "--metric", "120"]) == 0
    out = capsys.readouterr().out
    assert "FOUNDER APPROVAL NEEDED" in out and "Final outcome : STOP" in out
    log_path = next((tmp_path / "runs").iterdir()) / "decision_log.json"
    actions = [e["action"] for e in json.loads(log_path.read_text(encoding="utf-8"))]
    assert "approval_denied" in actions and "execute_publish" not in actions


def test_repo_demo_artifacts_are_untouched():
    assert json.loads((ROOT / "decision_log.json").read_text(encoding="utf-8")) == []
    assert [p.name for p in (ROOT / "outputs").iterdir()] == [".gitkeep"]
