"""scripts/demo_run.py (merge/rename logic) and scripts/validate_demo.py, on temp folders. No Ollama, no web."""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module   # dataclasses look the module up while the class is built
    spec.loader.exec_module(module)
    return module


demo_run = load("demo_run")
validate_demo = load("validate_demo")
URL = "https://www.reddit.com/r/translator/"


def entry(agent, action, run_id, sources=None, model=None, approved_by=None, **extra):
    e = {"timestamp": "2026-11-17T09:00:00Z", "agent": agent, "action": action, "sources": sources or [],
         "model": model, "cost": 0, "approved_by": approved_by, "run_id": run_id}
    e.update(extra)
    return e


def make_run(root: Path, run_id: str, phase2: bool, long_text: str = "") -> Path:
    """A run folder shaped like orchestrator.py's non-demo output."""
    run = root / "runs" / run_id
    run.mkdir(parents=True)
    (run / "sources").mkdir()
    (run / "phase1_step1_opportunities.md").write_text(
        f"> run_id: {run_id} | model: qwen3:8b | status: WORKING\n\nTranslators ask for trackers "
        f"[source]({URL}).\n", encoding="utf-8")
    log = [
        entry("orchestrator", "start_run", run_id, demo=False),
        entry("researcher", "web_search", run_id, sources=[URL], query="translator tracker", note=long_text),
        entry("researcher", "run_opportunities", run_id, sources=[URL], model="qwen3:8b", status="WORKING",
              tokens=1200, output_file="phase1_step1_opportunities.md", phase=1, step=1),
    ]
    if not phase2:
        (run / "phase1_gate_discover.md").write_text(
            f"> run_id: {run_id}\n\n# Gate\n\n**STOP. This did not pass.**\n", encoding="utf-8")
        log += [entry("gate", "discover", run_id, outcome="stop", basis="WORKING", phase=1, step=5,
                      output_file="phase1_gate_discover.md"),
                entry("gate", "founder_confirm", run_id, approved_by="founder", outcome="stop", gate="discover",
                      phase=1, step=5),
                entry("orchestrator", "end_run", run_id, status="stopped", outcome="stop", tokens=1200)]
    else:
        (run / "phase1_gate_discover.md").write_text(f"> run_id: {run_id}\n\n**GO. The target was met.**\n",
                                                     encoding="utf-8")
        (run / "phase2_step2_signups.md").write_text(
            f"> run_id: {run_id} | status: MOCKED\n\nMOCKED evidence: 12 of 400 visitors.\n", encoding="utf-8")
        (run / "phase2_gate_validate.md").write_text(f"> run_id: {run_id}\n\n**STOP (MOCKED).**\n",
                                                     encoding="utf-8")
        log += [entry("gate", "discover", run_id, outcome="go", phase=1, step=5),
                entry("gate", "founder_confirm", run_id, approved_by="founder", outcome="go", phase=1, step=5),
                entry("approval", "approval_request", run_id, kind="publish", summary="publish page"),
                entry("approval", "approval_granted", run_id, approved_by="founder", kind="publish",
                      summary="publish page", decision="approved"),
                entry("approval", "execute_publish", run_id, approved_by="founder", kind="publish",
                      summary="publish page"),
                entry("validator", "run_signups", run_id, status="MOCKED", output_file="phase2_step2_signups.md",
                      phase=2, step=2),
                entry("gate", "validate", run_id, outcome="stop", basis="MOCKED", phase=2, step=3),
                entry("gate", "founder_confirm", run_id, approved_by="founder", outcome="stop", phase=2, step=3),
                entry("orchestrator", "end_run", run_id, status="stopped", outcome="stop", tokens=3400)]
    (run / "decision_log.json").write_text(json.dumps(log, indent=2), encoding="utf-8")
    (run / "state.json").write_text("{}", encoding="utf-8")
    return run


def fake_runner(long_text=""):
    """Stands in for the orchestrator subprocess: creates the run folder a real run would."""
    calls = []

    def run(root, args):
        calls.append(list(args))
        phase2 = "1,2" in args
        make_run(Path(root), f"20261117T09000{len(calls)}Z-abc{len(calls)}", phase2, long_text)
        return 0
    run.calls = calls
    return run


# --- demo_run: helpers -------------------------------------------------------------

def test_suffixed_names_match_the_rule():
    assert demo_run.suffixed_name("phase1_gate_discover.md", "weak") == "phase1_gate_discover_weak.md"
    assert demo_run.suffixed_name("phase1_step3_scores_attempt2.md", "decent") == \
        "phase1_step3_scores_attempt2_decent.md"
    assert demo_run.NAME_RE.match("phase2_step4_landing_page_decent.html")
    assert not demo_run.NAME_RE.match("README.md")


def test_cli_args_follow_the_m3_contract():
    a, b = demo_run.DEMOS
    assert a.cli_args() == ["--brief", "examples/briefs/weak_idea.json", "--phases", "1"]
    assert b.cli_args(fast=True) == ["--brief", "examples/briefs/decent_idea.json", "--phases", "1,2",
                                     "--mock-signups", "12", "--mock-visitors", "400", "--fast"]
    assert "--demo" not in a.cli_args() + b.cli_args()


def test_missing_flags_are_reported():
    old_help = "usage: orchestrator.py [-h] --brief BRIEF [--metric METRIC] [--fast] [--demo]"
    assert demo_run.missing_flags(old_help, demo_run.DEMOS) == ["--phases", "--mock-signups", "--mock-visitors"]
    new_help = old_help + " [--phases PHASES] [--mock-signups N] [--mock-visitors V]"
    assert demo_run.missing_flags(new_help, demo_run.DEMOS) == []


def test_check_ollama_messages():
    ok, msg = demo_run.check_ollama("http://x:11434", "qwen3:8b", status=lambda: {"state": "ready"})
    assert ok
    ok, msg = demo_run.check_ollama("http://x:11434", "qwen3:8b", status=lambda: {"state": "loading"})
    assert not ok and "ollama pull qwen3:8b" in msg

    def down():
        raise ConnectionError("refused")
    for status in (down, lambda: {"state": "offline"}):
        ok, msg = demo_run.check_ollama("http://x:11434", "qwen3:8b", status=status)
        assert not ok and "not reachable" in msg


def test_orchestrator_help_is_read_in_process():
    text = demo_run.orchestrator_help()
    assert "--brief" in text


def test_preflight_refuses_non_interactive_input_and_old_orchestrator(tmp_path):
    problems = demo_run.preflight(ROOT, demo_run.DEMOS, stdin_is_tty=False, host="http://x", model="qwen3:8b",
                                  status=lambda: {"state": "ready"}, help_text="--brief --fast")
    text = " ".join(problems)
    assert "interactive terminal" in text and "--phases" in text


def test_main_writes_nothing_when_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(demo_run, "check_ollama", lambda *a, **k: (False, "Ollama is not reachable"))
    monkeypatch.setattr(demo_run, "orchestrator_help", lambda root: "")
    assert demo_run.main([], root=tmp_path) == 2   # pytest's stdin is not a terminal either
    assert list(tmp_path.iterdir()) == []


def test_there_is_no_flag_that_answers_for_the_founder():
    src = (ROOT / "scripts" / "demo_run.py").read_text(encoding="utf-8")
    assert not re.search(r"--(yes|auto|approve|non-interactive)", src)
    assert "DenyAllApprover" not in src and "PendingApprover" not in src


def test_collect_and_copy_keep_content_and_skip_non_outputs(tmp_path):
    run = make_run(tmp_path, "r1", phase2=False)
    (run / "phase1_step9_huge.md").write_text("x" * 100_001, encoding="utf-8")
    (run / "phase1_step9_Bad-Name.md").write_text("x", encoding="utf-8")
    pairs, skipped = demo_run.collect_run_files(run, "weak")
    names = [n for _, n in pairs]
    assert names == ["phase1_gate_discover_weak.md", "phase1_step1_opportunities_weak.md"]
    assert {n for n, _ in skipped} == {"phase1_step9_huge.md", "phase1_step9_Bad-Name.md"}
    copied = demo_run.copy_run_files(pairs, tmp_path / "outputs")
    for src, new in pairs:
        assert (tmp_path / "outputs" / new).read_bytes() == src.read_bytes()
    assert copied == names


def test_clean_previous_removes_only_our_files(tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()
    for n in ("phase1_gate_discover_weak.md", "phase2_step1_x_decent.md", demo_run.SUMMARY_NAME,
              "payment_test_log.md", "phase1_step1_other.md"):
        (out / n).write_text("x", encoding="utf-8")
    removed = demo_run.clean_previous_demo_files(out, ["weak", "decent"])
    assert sorted(p.name for p in out.iterdir()) == ["payment_test_log.md", "phase1_step1_other.md"]
    assert len(removed) == 3


def test_fit_log_trims_text_but_keeps_every_entry():
    big = [entry("researcher", f"a{i}", "r", sources=[URL], prompt="p" * 5000, evidence={"t": "q" * 3000})
           for i in range(100)]
    fitted, fits = demo_run.fit_log(big, max_bytes=300_000)
    assert fits and len(fitted) == 100
    assert demo_run.log_size(fitted) < 300_000
    assert [e["action"] for e in fitted] == [e["action"] for e in big]
    assert all(e["sources"] == [URL] for e in fitted)
    assert "trimmed" in fitted[0]["prompt"]


# --- demo_run: the whole session with a fake orchestrator -----------------------------

def test_session_resets_once_and_both_demos_append(tmp_path):
    (tmp_path / "decision_log.json").write_text(json.dumps([entry("old", "stale", "old")]), encoding="utf-8")
    runner = fake_runner(long_text="z" * 50_000)
    results, ok = demo_run.run_session(tmp_path, demo_run.DEMOS, runner=runner, model="qwen3:8b",
                                       host="http://localhost:11434", echo=lambda *a: None)
    assert ok, [r.problems for r in results]
    assert runner.calls[0] == demo_run.DEMOS[0].cli_args() and "1,2" in runner.calls[1]
    log = json.loads((tmp_path / "decision_log.json").read_text(encoding="utf-8"))
    actions = [e["action"] for e in log]
    assert "stale" not in actions                        # reset once at the start
    assert actions[0] == "start_demo_session" and actions[-1] == "end_demo_session"
    assert {e.get("demo") for e in log if e["agent"] != "demo_runner"} == {"A_weak", "B_decent"}
    assert len({e["run_id"] for e in log if e["agent"] != "demo_runner"}) == 2
    run_entries = sum(len(json.loads((p / "decision_log.json").read_text())) for p in (tmp_path / "runs").iterdir())
    assert len([e for e in log if e["agent"] != "demo_runner"]) == run_entries   # every action kept
    assert (tmp_path / "decision_log.json").stat().st_size < 300_000
    out = sorted(p.name for p in (tmp_path / "outputs").iterdir())
    assert "phase1_gate_discover_weak.md" in out and "phase2_step2_signups_decent.md" in out
    assert all(demo_run.NAME_RE.match(n) for n in out)
    assert results[0].outcome == "stop" and results[1].outcome == "stop"
    assert results[1].gates[0]["final"] == "go"
    summary = (tmp_path / "outputs" / demo_run.SUMMARY_NAME).read_text(encoding="utf-8")
    for needle in ("played the founder role", "MOCKED", "qwen3:8b", "Wall time", "Tokens", "weak_idea.json",
                   "decent_idea.json", "STOP"):
        assert needle in summary
    # the session's own output passes the validator
    problems, warnings, facts = validate_demo.validate(tmp_path)
    assert problems == [], problems
    assert facts["entries"] == len(log)


def test_session_reports_a_run_that_wrote_nothing(tmp_path):
    results, ok = demo_run.run_session(tmp_path, demo_run.DEMOS[:1], runner=lambda root, args: 1,
                                       model="m", host="h", echo=lambda *a: None)
    assert not ok
    assert any("no new run folder" in p for p in results[0].problems)
    assert any("exited with code 1" in p for p in results[0].problems)
    summary = (tmp_path / "outputs" / demo_run.SUMMARY_NAME).read_text(encoding="utf-8")
    assert "no new run folder" in summary   # the failure is shown, not hidden


# --- validate_demo -------------------------------------------------------------------

def committed_root(tmp_path):
    demo_run.run_session(tmp_path, demo_run.DEMOS, runner=fake_runner(), model="qwen3:8b", host="h",
                         echo=lambda *a: None)
    return tmp_path


def _rewrite_log(root, change):
    path = root / "decision_log.json"
    log = json.loads(path.read_text(encoding="utf-8"))
    change(log)
    path.write_text(json.dumps(log), encoding="utf-8")


def test_validate_flags_a_missing_key(tmp_path):
    root = committed_root(tmp_path)
    _rewrite_log(root, lambda log: log[1].pop("approved_by"))
    problems, _, _ = validate_demo.validate(root)
    assert any("missing ['approved_by']" in p for p in problems)


def test_validate_flags_an_unanswered_approval_request(tmp_path):
    root = committed_root(tmp_path)
    _rewrite_log(root, lambda log: log.append(entry("approval", "approval_request", "r9", kind="send",
                                                    summary="send email")))
    problems, _, _ = validate_demo.validate(root)
    assert any("no founder approval or recorded rejection" in p for p in problems)


def test_validate_accepts_a_recorded_rejection(tmp_path):
    root = committed_root(tmp_path)
    _rewrite_log(root, lambda log: log.extend([
        entry("approval", "approval_request", "r9", kind="send", summary="s"),
        entry("approval", "approval_denied", "r9", kind="send", summary="s", decision="rejected")]))
    assert validate_demo.validate(root)[0] == []


def test_validate_flags_an_execution_without_founder(tmp_path):
    root = committed_root(tmp_path)
    _rewrite_log(root, lambda log: log.append(entry("approval", "execute_publish", "r9", kind="publish",
                                                    summary="x")))
    problems, _, _ = validate_demo.validate(root)
    assert any("executed action must have approved_by" in p for p in problems)


def test_validate_flags_unlogged_urls_bad_names_big_files_and_missing_mocked(tmp_path):
    root = committed_root(tmp_path)
    out = root / "outputs"
    (out / "phase1_step1_opportunities_weak.md").write_text("See https://example.com/not-logged.\n",
                                                             encoding="utf-8")
    (out / "notes.md").write_text("x", encoding="utf-8")
    (out / "phase1_step7_big_weak.md").write_text("a" * 100_000, encoding="utf-8")
    (out / "phase2_step2_signups_decent.md").write_text("12 sign-ups from 400 visitors\n", encoding="utf-8")
    problems, _, _ = validate_demo.validate(root)
    text = "\n".join(problems)
    assert "https://example.com/not-logged is not in any log entry's sources" in text
    assert "outputs/notes.md: the name does not match" in text
    assert "phase1_step7_big_weak.md: 100000 bytes" in text
    assert "phase2_step2_signups_decent.md: built from mocked input but does not say MOCKED" in text


def test_validate_needs_a_stop_gate(tmp_path):
    root = committed_root(tmp_path)
    for p in (root / "outputs").glob("phase*_gate_*"):
        p.write_text("GO\n", encoding="utf-8")
    problems, _, _ = validate_demo.validate(root)
    assert "no gate file in outputs/ says STOP" in problems


def test_validate_main_exit_codes(tmp_path, capsys):
    assert validate_demo.main(["--root", str(tmp_path)]) == 1
    assert "FAIL" in capsys.readouterr().out
    root = committed_root(tmp_path / "ok")
    assert validate_demo.main(["--root", str(root)]) == 0
    assert "PASS" in capsys.readouterr().out
