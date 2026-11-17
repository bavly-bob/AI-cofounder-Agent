"""M3 Phase 2 "Validate" tests. No network and no Ollama: FakeLLM (MOCKED model replies) and
canned web results from tests/fixtures/. The approvers here are test-only (tests/ only)."""
import copy
import json
import re
from pathlib import Path

import pytest

import guardrails
import orchestrator
from agents.copywriter import MOCK_LABEL, STATUS_LINE, build_html, check_page
from agents.experimenter import change_target, register_target, target_hash
from approval import ApprovalResult, Approver, DenyAllApprover, GateReview
from brief import FounderBrief, load_brief
from decision_log import DecisionLogger
from gates import Outcome, Phase2DemandGate, load_phase2_thresholds
from llm import FakeLLM
from orchestrator import run_pipeline
from steps import DummyValidateStep, RunContext, StepResult, registered_steps

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"
BRIEFS = ROOT / "examples" / "briefs"
WEB = json.loads((FIX / "discover" / "web.json").read_text(encoding="utf-8"))
REPLIES = json.loads((FIX / "discover" / "fake_llm_decent.json").read_text(encoding="utf-8"))
REPLIES.update(json.loads((FIX / "validate" / "fake_llm_phase2.json").read_text(encoding="utf-8")))
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class ScriptedApprover(Approver):
    """Test-only approver: answers each request kind from a script (default: approve)."""

    def __init__(self, answers=None, approve=True, note="scripted test answer"):
        self.answers, self.approve, self.note, self.requests = dict(answers or {}), approve, note, []

    def request(self, kind, summary, payload):
        self.requests.append(kind)
        answer = self.answers.get(kind, self.approve)
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        return ApprovalResult(bool(answer), "founder", self.note)

    def review_gate(self, summary, system_outcome, options):
        return GateReview(None, None, "scripted: no decision")


def search(query, k=5, errors=None):
    return [dict(r) for r in WEB["results"][:k]]


def fetch(url):
    if url in WEB["pages"]:
        return {"url": url, "title": "page", "text": WEB["pages"][url]}
    return {"url": url, "title": "", "text": "", "error": "no network (test)"}


def run(tmp_path, replies=None, approver=None, **kw):
    llm = FakeLLM(copy.deepcopy(replies or REPLIES))
    summary = run_pipeline(load_brief(BRIEFS / "decent_idea.json"), base_dir=tmp_path, llm_client=llm,
                           search_fn=search, fetch_fn=fetch, fast=True,
                           approver=approver if approver is not None else ScriptedApprover(), **kw)
    return summary, llm


def log_of(summary):
    return json.loads(summary.log_path.read_text(encoding="utf-8"))


def read(summary, name):
    return (summary.output_dir / name).read_text(encoding="utf-8")


def validate_rows(summary):
    return [g for g in summary.gates if g["gate"] == "validate"]


def phase2_files(summary):
    return sorted(p.name for p in summary.output_dir.iterdir() if p.name.startswith("phase2_"))


# --- registry -----------------------------------------------------------------------------

def test_phase2_agents_replace_the_dummy_validate_step():
    steps = registered_steps()
    phase2 = [(s.phase, s.step, s.agent) for s in steps if s.phase == 2]
    assert phase2 == [(2, 1, "strategist"), (2, 2, "experimenter"), (2, 3, "copywriter"), (2, 4, "analyst")]
    assert DummyValidateStep not in [type(s) for s in steps]
    for role in ("strategist", "experimenter", "copywriter", "analyst"):
        assert (ROOT / "agents" / f"{role}.py").is_file()
    for role in ("strategist", "experimenter", "copywriter"):   # the analyst uses no model
        assert (ROOT / "prompts" / f"{role}.md").is_file()


# --- 2.1 lean canvas --------------------------------------------------------------------------

def test_canvas_has_nine_boxes_and_unsourced_items_are_assumptions(tmp_path):
    summary, _ = run(tmp_path, phases=[1, 2])
    text = read(summary, "phase2_step1_lean_canvas.md")
    for title in ("Problem", "Customer segments", "Unique value proposition", "Solution", "Channels",
                  "Revenue streams", "Cost structure", "Key metrics", "Unfair advantage"):
        assert f"| {title} |" in text, title
    assert "Translators with several agencies lose track of unpaid invoices. [source](https://www.reddit.com" in text
    assert "Freelance translators with three or more agency clients (ASSUMPTION)" in text
    assert "One-time template price, to be tested (ASSUMPTION)" in text        # revenue: never sourced
    assert "None yet (ASSUMPTION)" in text
    assert "Projected revenue" not in text and "5000" not in text              # projection dropped
    assert "Price hypothesis (ASSUMPTION" in text
    assert text.count("Cheap test:") == 3
    assert "drop_projection" in [e["action"] for e in log_of(summary)]


def test_canvas_rejection_repeats_the_step_with_the_founders_note(tmp_path):
    approver = ScriptedApprover({"sign": [False, True]}, note="focus on invoices only")
    summary, llm = run(tmp_path, approver=approver, phases=[1, 2])
    canvas_gates = [g for g in summary.gates if g["gate"] == "founder_approval_canvas"]
    assert [g["outcome"] for g in canvas_gates] == ["repeat", "go"]
    prompts = [c["prompt"] for c in llm.calls if c["name"] == "strategist.canvas"]
    assert len(prompts) == 2 and "focus on invoices only" in prompts[1] and "focus on invoices" not in prompts[0]
    assert (summary.output_dir / "phase2_step1_lean_canvas_attempt2.md").exists()


def test_unsigned_canvas_stops_before_the_experiment(tmp_path):
    summary, _ = run(tmp_path, approver=DenyAllApprover(), phases=[1, 2])
    assert summary.final_outcome == "stop"
    assert not (summary.output_dir / "phase2_step2_experiment.md").exists()


# --- 2.2 experiment and the pre-registered target -------------------------------------------

def test_target_is_approved_before_results_and_logged_with_its_hash(tmp_path):
    summary, _ = run(tmp_path, phases=[1, 2], mock_signups=12, mock_visitors=400)
    entries = log_of(summary)
    actions = [e["action"] for e in entries]
    reg = next(e for e in entries if e["action"] == "register_experiment_target")
    assert reg["approved_by"] == "founder"
    assert reg["target"] == {"metric": "waitlist_signups", "signups": 100, "window_days": 14, "min_conversion": 0.05}
    assert reg["sha256"] == target_hash(reg["target"]) and len(reg["sha256"]) == 64
    assert actions.index("register_experiment_target") < actions.index("run_demand_results")
    exp = read(summary, "phase2_step2_experiment.md")
    assert ">= 100 sign-ups in 14 days, conversion >= 5%" in exp and "Kill criteria" in exp


def _ctx(tmp_path, approver=None):
    logger = DecisionLogger(tmp_path / "log.json", run_id="t")
    return RunContext(brief=FounderBrief("an idea"), run_id="t", logger=logger,
                      approver=approver or DenyAllApprover(), output_dir=tmp_path)


def test_target_change_after_approval_is_refused_and_the_hash_is_unchanged(tmp_path):
    ctx = _ctx(tmp_path)
    target = {"metric": "waitlist_signups", "signups": 100, "window_days": 14, "min_conversion": 0.05}
    record = register_target(ctx, target)
    before = record["sha256"]
    assert change_target(ctx, dict(target, signups=10), "lower the target so we pass") is False
    assert ctx.options["experiment_target"]["sha256"] == before == target_hash(target)
    assert ctx.options["experiment_target"]["target"]["signups"] == 100
    refused = [e for e in ctx.logger.entries() if e["action"] == "refuse_target_change"]
    assert refused and refused[0]["requested"]["signups"] == 10 and refused[0]["sha256"] == before


def test_a_tampered_target_is_a_stop(tmp_path):
    ctx = _ctx(tmp_path)
    register_target(ctx, {"metric": "waitlist_signups", "signups": 100, "window_days": 14, "min_conversion": 0.05})
    ctx.options["experiment_target"]["target"]["signups"] = 10      # edited behind the log's back
    decision = Phase2DemandGate().evaluate(_demand_result(signups=12, visitors=100), ctx)
    assert decision.outcome == Outcome.STOP and "changed after approval" in decision.reasons[0]


def test_agent_may_only_propose_a_stricter_target():
    from agents.experimenter import propose_target
    config = load_phase2_thresholds()
    assert propose_target(config, 40, "small market")[0]["signups"] == 100
    assert propose_target(config, 150, "large active forums")[0]["signups"] == 150
    assert propose_target(config, 150, "")[0]["signups"] == 100


# --- 2.3 landing page -------------------------------------------------------------------------

COPY = {"product_name": "Agency Desk", "headline": "Every agency job in one place",
        "problem": "Many portals and formats.", "how_it_works": ["Copy it.", "Add jobs.", "See deadlines."],
        "who_its_for": "Freelance translators."}


def test_generated_page_passes_the_check_and_is_honest(tmp_path):
    summary, _ = run(tmp_path, phases=[1, 2])
    page = read(summary, "phase2_step3_landing_page.html")
    assert check_page(page)[0].allowed
    assert MOCK_LABEL in page and STATUS_LINE.replace("'", "&#x27;") in page
    assert "<script" not in page.lower() and "http" not in page.split("-->", 1)[1]
    assert 'name="viewport"' in page and 'type="email"' in page and 'type="checkbox"' in page
    assert "<form>" in page and "action=" not in page and "disabled" in page
    assert "Privacy note" in page
    assert page.splitlines()[1].startswith(f"<!-- run_id: {summary.run_id} | model:")


@pytest.mark.parametrize("bad", [
    '<blockquote>"It changed my work!" - Ana, translator. Testimonial</blockquote>',
    "<p>★★★★★</p>",
    "<script>var t=setInterval(function(){countdown()},1000)</script>",
    "<p>Join 5,000 founders on the waitlist.</p>",
    "<p>Earn $2,000 more every month.</p>",
])
def test_landing_page_check_blocks_dishonest_content(bad):
    page = build_html(COPY, "t", None).replace("</main>", bad + "</main>")
    assert check_page(page)[0].allowed is False
    assert hasattr(guardrails, "check_landing_page")


def test_publish_denied_writes_nothing(tmp_path):
    approver = ScriptedApprover({"publish": False})
    summary, _ = run(tmp_path, approver=approver, phases=[1, 2], mock_signups=130, mock_visitors=1500)
    assert approver.requests == ["send", "sign", "sign", "publish"]
    assert not (tmp_path / "runs" / summary.run_id / "published_mock").exists()
    assert summary.final_outcome == "stop" and "execute_publish" not in [e["action"] for e in log_of(summary)]


def test_publish_with_deny_all_approver_writes_nothing(tmp_path):
    from agents.copywriter import publish_payload
    from gates import FounderApprovalGate
    from agents.copywriter import mock_publish_page
    ctx = _ctx(tmp_path)
    ctx.options["run_dir"] = str(tmp_path / "run")
    result = StepResult(2, 3, "landing_page", "copywriter", "x", [], {"html": build_html(COPY, "t", None)})
    decision = FounderApprovalGate("publish", "publish", mock_publish_page, publish_payload).evaluate(result, ctx)
    assert decision.outcome == Outcome.STOP and not (tmp_path / "run" / "published_mock").exists()


def test_publish_approved_writes_only_the_local_mock_copy(tmp_path):
    summary, _ = run(tmp_path, phases=[1, 2], mock_signups=130, mock_visitors=1500)
    copy_path = tmp_path / "runs" / summary.run_id / "published_mock" / "landing_page.html"
    assert copy_path.read_text(encoding="utf-8") == read(summary, "phase2_step3_landing_page.html")
    pub = next(e for e in log_of(summary) if e["action"] == "execute_publish")
    assert pub["approved_by"] == "founder" and pub["status"] == "MOCKED"


# --- 2.4 demand results and the Phase 2 gate ----------------------------------------------------

def _demand_result(signups=None, visitors=None, mocked=False, kind="founder_reported"):
    conv = round(signups / visitors, 4) if signups is not None and visitors else None
    ev = {"type": kind if signups is not None else "none", "signups": signups, "visitors": visitors,
          "conversion": conv, "days_elapsed": 14, "window_days": 14, "mocked": mocked}
    return StepResult(2, 4, "demand_results", "analyst", "x", [], {"evidence": ev})


def test_no_data_is_stop_with_the_rerun_hint(tmp_path):
    summary, _ = run(tmp_path, phases=[1, 2])
    assert [g["system_outcome"] for g in validate_rows(summary)] == ["stop"]
    gate = read(summary, "phase2_gate_validate.md")
    assert "demand test not run yet" in gate and f"--resume {summary.run_id} --from phase2 --signups" in gate
    assert "Actual: no evidence" in gate and "approved by the founder at" in gate


def test_12_of_100_mocked_is_stop_and_mocked_everywhere(tmp_path):
    summary, _ = run(tmp_path, phases=[1, 2], mock_signups=12, mock_visitors=400)
    assert [g["system_outcome"] for g in validate_rows(summary)] == ["stop"] and summary.final_outcome == "stop"
    gate = read(summary, "phase2_gate_validate.md")
    assert "**Outcome: STOP (MOCKED" in gate and "12 sign-ups (12% of the target)" in gate
    assert "conversion 3.0%" in gate and "evidence: MOCKED evidence" in gate
    assert "## What evidence would change this decision" in gate and "## Founder decision" in gate
    for name in phase2_files(summary):
        assert "MOCKED" in read(summary, name), name


def test_12_of_100_pivots_when_a_strong_alternative_exists(tmp_path):
    ctx = _ctx(tmp_path)
    register_target(ctx, {"metric": "waitlist_signups", "signups": 100, "window_days": 14, "min_conversion": 0.05})
    ctx.results.append(StepResult(1, 2, "idea_scoring", "scorer", "x", [], {"ideas": [
        {"id": "main", "name": "main", "total": 3.0, "signals": 5},
        {"id": "alt1", "name": "Payment-chasing templates", "short": "Payment-chasing templates",
         "total": 4.1, "signals": 3}]}))
    decision = Phase2DemandGate().evaluate(_demand_result(12, 400), ctx)
    assert decision.outcome == Outcome.PIVOT and "Payment-chasing templates" in " ".join(decision.reasons)


def test_70_of_100_repeats_once_then_pivots(tmp_path):
    summary, llm = run(tmp_path, phases=[1, 2], mock_signups=70, mock_visitors=1000)
    assert [g["system_outcome"] for g in validate_rows(summary)] == ["repeat", "pivot"]
    assert summary.final_outcome == "pivot"
    entries = log_of(summary)
    assert [e for e in entries if e["action"] == "new_experiment"]           # the only way to change a target
    designs = [c["prompt"] for c in llm.calls if c["name"] == "experimenter.design"]
    assert len(designs) == 2 and "Change the channel or the message" in designs[1]
    assert (summary.output_dir / "phase2_gate_validate_attempt2.md").exists()
    assert "already repeated 1 time(s), so this is a PIVOT" in read(summary, "phase2_gate_validate_attempt2.md")


def test_120_of_100_at_good_conversion_is_go(tmp_path):
    summary, _ = run(tmp_path, phases=[1, 2], signups=120, visitors=1000)
    assert [g["system_outcome"] for g in validate_rows(summary)] == ["go"] and summary.final_outcome == "go"
    gate = read(summary, "phase2_gate_validate.md")
    assert "**Outcome: GO. The target was met.**" in gate and "evidence: founder-reported" in gate
    assert "MOCKED" not in gate


def test_target_met_with_low_conversion_is_not_go(tmp_path):
    ctx = _ctx(tmp_path)
    register_target(ctx, {"metric": "waitlist_signups", "signups": 100, "window_days": 14, "min_conversion": 0.05})
    assert Phase2DemandGate().evaluate(_demand_result(120, 5000), ctx).outcome == Outcome.REPEAT
    assert Phase2DemandGate().evaluate(_demand_result(120, None), ctx).outcome == Outcome.REPEAT


def test_mocked_go_says_not_a_real_validation_in_every_artifact(tmp_path):
    summary, _ = run(tmp_path, phases=[1, 2], mock_signups=130, mock_visitors=1500)
    assert summary.final_outcome == "go"
    gate = read(summary, "phase2_gate_validate.md")
    assert "**Outcome: GO (MOCKED — not a real validation)" in gate
    files = phase2_files(summary)
    assert {"phase2_step1_lean_canvas.md", "phase2_step2_experiment.md", "phase2_step3_landing_page.md",
            "phase2_step3_landing_page.html", "phase2_step4_demand_results.md", "phase2_gate_validate.md"} <= set(files)
    for name in files:
        assert "MOCKED" in read(summary, name), name
    assert "MOCKED evidence — demonstration only" in read(summary, "phase2_step4_demand_results.md")
    assert "status: MOCKED" in read(summary, "phase2_step4_demand_results.md").splitlines()[0]


def test_waitlist_csv_counts_only_and_no_email_is_written_anywhere(tmp_path):
    csv_path = tmp_path / "waitlist.csv"
    csv_path.write_text("Email,Signed up at\n"
                        "ana@example.com,2099-01-01\n"
                        "ANA@example.com,2099-01-01\n"           # duplicate
                        "not-an-email,2099-01-01\n"             # invalid
                        "bo@example.org,2000-01-01\n"           # outside the window
                        "cy@example.net,\n"                     # undated: counted
                        "di@example.co.uk,2099-01-02\n", encoding="utf-8")
    from agents.analyst import count_waitlist
    from datetime import datetime, timezone
    counts = count_waitlist(csv_path, datetime(2026, 1, 1, tzinfo=timezone.utc), window_days=100000)
    assert counts == {"rows": 6, "valid": 4, "invalid_emails": 1, "duplicates": 1, "outside_window": 1,
                      "undated": 1, "counted": 3, "truncated": False}
    summary, _ = run(tmp_path / "r", phases=[1, 2], waitlist_path=csv_path, visitors=50,
                     mock_signups=999, mock_visitors=1000)                # a real export outranks MOCKED input
    text = read(summary, "phase2_step4_demand_results.md")
    assert "waitlist export (CSV)" in text and "MOCKED evidence" not in text
    for path in (tmp_path / "r").rglob("*"):
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            assert not [m for m in EMAIL_RE.findall(content) if "example" in m], path


# --- resume -----------------------------------------------------------------------------------

def test_resume_from_phase2_reuses_phase1_and_the_approved_target(tmp_path):
    first, _ = run(tmp_path, phases=[1, 2])
    assert first.final_outcome == "stop"
    first_target = next(e for e in log_of(first) if e["action"] == "register_experiment_target")["sha256"]
    llm = FakeLLM(copy.deepcopy(REPLIES))
    approver = ScriptedApprover()
    second = run_pipeline(None, base_dir=tmp_path, llm_client=llm, search_fn=search, fetch_fn=fetch,
                          approver=approver, resume_run_id=first.run_id, from_phase="phase2",
                          signups=120, visitors=1000)
    assert second.run_id == first.run_id and second.final_outcome == "go"
    assert llm.calls == [] and approver.requests == []        # Phase 1 and the approved 2.1-2.3 are reused
    assert [s["id"] for s in second.steps] == ["2.4"]
    entries = log_of(second)
    assert [e for e in entries if e["action"] == "reuse_saved_step"]
    assert not [e for e in entries if e["action"] == "register_experiment_target"][1:]   # the same target
    gate = read(second, "phase2_gate_validate_attempt2.md")
    assert "GO. The target was met" in gate and first_target[:16] in gate
    assert (second.output_dir / "phase2_gate_validate.md").exists()   # the earlier decision is kept


def test_resume_with_no_data_is_stop_not_run_yet(tmp_path):
    first, _ = run(tmp_path, phases=[1, 2], mock_signups=12, mock_visitors=400)
    second = run_pipeline(None, base_dir=tmp_path, llm_client=FakeLLM({}), search_fn=search, fetch_fn=fetch,
                          resume_run_id=first.run_id, from_phase=2)
    assert second.final_outcome == "stop"
    assert "demand test not run yet" in read(second, "phase2_gate_validate_attempt2.md")
    assert (second.output_dir / "phase2_gate_stop_memo.md").exists()
    assert (second.output_dir / "phase2_gate_stop_memo_attempt2.md").exists()


def test_resume_refuses_phase2_without_a_phase1_go(tmp_path):
    replies = copy.deepcopy(REPLIES)
    replies["researcher.extract"]["pain_signals"] = replies["researcher.extract"]["pain_signals"][:1]
    first, _ = run(tmp_path, replies, phases=[1, 2])
    assert first.final_outcome == "stop" and not phase2_files(first)
    second = run_pipeline(None, base_dir=tmp_path, llm_client=FakeLLM({}), search_fn=search, fetch_fn=fetch,
                          resume_run_id=first.run_id, from_phase=2, signups=500, visitors=1000)
    assert second.final_outcome == "stop" and not second.steps
    assert "Phase 2 starts only after a Phase 1 GO" in " ".join(second.stop_reasons)


def test_resume_of_an_unknown_run_is_a_plain_error(tmp_path):
    with pytest.raises(orchestrator.ResumeError):
        run_pipeline(None, base_dir=tmp_path, resume_run_id="20990101T000000Z-nope", from_phase=2)


def test_cli_phases_1_2_prints_the_total_time(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    answers = iter(["y"] * 10)
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    code = orchestrator.main(["--brief", str(BRIEFS / "decent_idea.json"), "--phases", "1,2", "--fast",
                              "--mock-signups", "12", "--mock-visitors", "400"],
                             llm_client=FakeLLM(copy.deepcopy(REPLIES)), search_fn=search, fetch_fn=fetch)
    out = capsys.readouterr().out
    assert code == 0 and "Final outcome : STOP" in out
    assert re.search(r"Total run time: \d+\.\ds \(--phases 1,2 --fast\)", out)
    assert orchestrator.main(["--brief", str(BRIEFS / "decent_idea.json"), "--phases", "2"]) == 2


# --- orchestrator fixes from the edge-brief tests -----------------------------------------------

def test_empty_idea_stops_and_asks_for_more_information_before_any_call(tmp_path):
    llm = FakeLLM({})
    calls = []
    summary = run_pipeline(FounderBrief(idea="  \U0001F680 "), base_dir=tmp_path, llm_client=llm,
                           search_fn=lambda *a, **k: calls.append(a) or [], fetch_fn=lambda u: calls.append(u))
    assert summary.final_outcome == "stop" and summary.status == "stopped"
    assert llm.calls == [] and calls == [] and not summary.steps
    assert "Please describe the idea" in summary.stop_reasons[0]
    assert "need_more_info" in [e["action"] for e in log_of(summary)]


def test_unloadable_json_brief_leaves_a_log_entry_and_a_memo(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    code = orchestrator.main(["--brief", str(FIX / "briefs_edge" / "empty.json")])
    err = capsys.readouterr().err
    assert code == 2 and "Could not load the brief" in err and "no 'idea'" in err
    logs = list((tmp_path / "runs").glob("*/decision_log.json"))
    assert len(logs) == 1
    actions = [e["action"] for e in json.loads(logs[0].read_text(encoding="utf-8"))]
    assert actions[0] == "start_run" and "need_more_info" in actions and actions[-1] == "end_run"
    assert (logs[0].parent / "phase0_gate_stop_memo.md").is_file()


def test_empty_free_text_brief_stops_without_a_model_call(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "parse_brief_text", lambda *a, **k: pytest.fail("no model call expected"))
    code = orchestrator.main(["--brief", str(FIX / "briefs_edge" / "empty.txt")])
    out = capsys.readouterr().out
    assert code == 0 and "Final outcome : STOP" in out and "Please describe the idea" in out


# --- M3 QA fix round (D-C, D-D, D-E, D-F) -----------------------------------------------------

@pytest.mark.parametrize("text", ["Earn up to 2000 per month", "Break-even in 3 months",
                                  "Year 1: 50,000 USD revenue", "2k in sales after launch",
                                  "Profitable within a quarter"])
def test_canvas_drops_projections_without_currency_symbols(text):
    from agents.strategist import _PROJECTION
    assert _PROJECTION.search(text), text


@pytest.mark.parametrize("text", ["A one-time price of about 19 USD, to be tested",
                                  "A subscription of 5 USD per month, to be tested",
                                  "Post in 3 networking groups", "Waitlist sign-ups and conversion"])
def test_canvas_keeps_honest_price_hypotheses_and_plain_items(text):
    from agents.strategist import _PROJECTION
    assert not _PROJECTION.search(text), text


def test_projection_in_a_canvas_box_is_dropped_and_logged(tmp_path):
    replies = copy.deepcopy(REPLIES)
    replies["strategist.canvas"]["revenue_streams"] = [{"text": "Earn up to 2000 per month", "source": ""},
                                                       {"text": "One-time template price", "source": ""}]
    replies["strategist.canvas"]["key_metrics"].append({"text": "Break-even in 3 months", "source": ""})
    summary, _ = run(tmp_path, replies, phases=[1, 2])
    text = read(summary, "phase2_step1_lean_canvas.md")
    assert "2000" not in text and "Break-even" not in text and "One-time template price (ASSUMPTION)" in text
    assert "Price hypothesis (ASSUMPTION" in text
    assert len([e for e in log_of(summary) if e["action"] == "drop_projection"]) >= 2


def test_unreadable_waitlist_is_reported_and_other_evidence_is_not_used(tmp_path):
    missing = tmp_path / "does_not_exist.csv"
    summary, _ = run(tmp_path / "r", phases=[1, 2], waitlist_path=missing, signups=500, visitors=1000)
    text = read(summary, "phase2_step4_demand_results.md")
    assert "could NOT be read (FileNotFoundError)" in text and "no other evidence was used instead" in text
    assert "(--signups) but was NOT used" in text and "- Sign-ups: no data" in text
    assert summary.final_outcome == "stop" and [g["system_outcome"] for g in validate_rows(summary)] == ["stop"]
    entry = next(e for e in log_of(summary) if e["action"] == "waitlist_unreadable")
    assert entry["ignored_evidence"] == ["--signups"] and str(tmp_path) not in json.dumps(entry)


def test_date_only_rows_on_the_approval_day_are_counted(tmp_path):
    from datetime import datetime, timezone
    from agents.analyst import count_waitlist
    csv_path = tmp_path / "w.csv"
    csv_path.write_text("email,date\n"
                        "a@example.com,2026-11-17\n"            # approval day (date only): counted
                        "b@example.com,17/11/2026\n"            # same day, other format: counted
                        "c@example.com,2026-12-01\n"            # last day of the 14-day window: counted
                        "d@example.com,2026-11-16\n"            # the day before: outside
                        "e@example.com,2026-12-02\n"            # after the window: outside
                        "f@example.com,2026-11-17T08:00:00Z\n", # a time before approval: outside
                        encoding="utf-8")
    approved = datetime(2026, 11, 17, 12, 39, tzinfo=timezone.utc)
    counts = count_waitlist(csv_path, approved, window_days=14)
    assert counts["counted"] == 3 and counts["outside_window"] == 3


def test_published_copy_path_is_logged_relative_to_the_run(tmp_path):
    summary, _ = run(tmp_path, phases=[1, 2], mock_signups=130, mock_visitors=1500)
    gate = next(e for e in log_of(summary) if e["action"] == "founder_approval_publish")
    assert gate["evidence"]["result"]["path"] == "published_mock/landing_page.html"
    assert str(tmp_path) not in json.dumps(gate)
