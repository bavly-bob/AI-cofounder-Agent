"""M3 QA fixes: a resume never re-grants the Phase 2 REPEAT (D-A), and the stored target is
cross-checked against the logged register_experiment_target entry. No network, no Ollama."""
import copy
import json

from agents.experimenter import target_hash
from llm import FakeLLM
from orchestrator import RESUME_DIR, run_pipeline
from tests.test_validate_m3 import REPLIES, ScriptedApprover, fetch, log_of, read, run, search, validate_rows


def _resume(tmp_path, run_id, **kw):
    return run_pipeline(None, base_dir=tmp_path, llm_client=FakeLLM(copy.deepcopy(REPLIES)), search_fn=search,
                        fetch_fn=fetch, approver=ScriptedApprover(), resume_run_id=run_id, from_phase=2, **kw)


def _resume_file(tmp_path, run_id):
    return tmp_path / "runs" / RESUME_DIR / f"{run_id}.json"


def test_resume_after_a_used_repeat_does_not_grant_another(tmp_path):
    first, _ = run(tmp_path, phases=[1, 2], signups=70, visitors=1000)
    assert [g["system_outcome"] for g in validate_rows(first)] == ["repeat", "pivot"]
    saved = json.loads(_resume_file(tmp_path, first.run_id).read_text(encoding="utf-8"))
    assert saved["options"]["phase2_repeats_used"] == 1 and saved["gate_finals"]["2.4"] == "pivot"

    second = _resume(tmp_path, first.run_id, signups=70, visitors=1000)
    assert [g["system_outcome"] for g in validate_rows(second)] == ["pivot"]      # no fresh REPEAT
    assert second.final_outcome == "pivot"
    gate_files = sorted(p.name for p in second.output_dir.glob("phase2_gate_validate*.md"))
    assert "already repeated 1 time(s), so this is a PIVOT" in read(second, gate_files[-1])
    # and again: the count survives every resume
    third = _resume(tmp_path, first.run_id, signups=70, visitors=1000)
    assert [g["system_outcome"] for g in validate_rows(third)] == ["pivot"]


def test_resume_without_a_used_repeat_may_still_repeat_once(tmp_path):
    first, _ = run(tmp_path, phases=[1, 2])                                       # no data: STOP, no REPEAT used
    assert first.final_outcome == "stop"
    second = _resume(tmp_path, first.run_id, signups=70, visitors=1000)
    assert [g["system_outcome"] for g in validate_rows(second)] == ["repeat", "pivot"]


def test_stored_target_that_differs_from_the_logged_one_is_stop(tmp_path):
    first, _ = run(tmp_path, phases=[1, 2])
    path = _resume_file(tmp_path, first.run_id)
    saved = json.loads(path.read_text(encoding="utf-8"))
    reg = saved["options"]["experiment_target"]
    reg["target"]["signups"] = 5                                                  # an easier target...
    reg["sha256"] = target_hash(reg["target"])                                   # ...with a matching hash
    path.write_text(json.dumps(saved), encoding="utf-8")

    second = _resume(tmp_path, first.run_id, signups=120, visitors=1000)
    assert second.final_outcome == "stop"
    assert [g["system_outcome"] for g in validate_rows(second)] == ["stop"]
    gate = next(e for e in reversed(log_of(second)) if e["action"] == "validate")
    assert "recorded in the run's decision log" in " ".join(gate["reasons"])


def test_untouched_target_still_passes_the_cross_check(tmp_path):
    first, _ = run(tmp_path, phases=[1, 2])
    second = _resume(tmp_path, first.run_id, signups=120, visitors=1000)
    assert second.final_outcome == "go"


def test_unreadable_waitlist_gate_reason_says_so(tmp_path):
    summary, _ = run(tmp_path / "r", phases=[1, 2], waitlist_path=tmp_path / "missing.csv", signups=500,
                     visitors=1000)
    gate = next(e for e in reversed(log_of(summary)) if e["action"] == "validate")
    assert gate["outcome"] == "stop"
    assert gate["reasons"][0].startswith("The --waitlist export could not be read (FileNotFoundError)")
    assert "no other evidence was used" in gate["reasons"][0]
