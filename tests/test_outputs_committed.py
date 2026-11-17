"""The committed demo run in outputs/ and decision_log.json. Skipped until the demo is committed."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
NAME_RE = re.compile(r"^phase\d+_(step\d+|gate)_[a-z0-9_]+\.(md|json|html)$")
EXTRA_NAMES = {"payment_test_log.md", ".gitkeep"}
REQUIRED_KEYS = ("timestamp", "agent", "action", "sources", "model", "cost", "approved_by")


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_demo", ROOT / "scripts" / "validate_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _phase_files():
    if not OUTPUTS.is_dir():
        return []
    return sorted(p for p in OUTPUTS.iterdir() if p.is_file() and p.name.startswith("phase"))


pytestmark = pytest.mark.skipif(
    not _phase_files(),
    reason="outputs/ has no phase files yet: the demo run (python scripts/demo_run.py, played by a human "
           "founder) has not been committed",
)


def _files():
    return sorted(p for p in OUTPUTS.iterdir() if p.is_file() and p.name != ".gitkeep")


def test_committed_output_names_match_the_rule():
    bad = [p.name for p in _files() if not (NAME_RE.match(p.name) or p.name in EXTRA_NAMES)]
    assert not bad, f"output names that break the naming rule: {bad}"


def test_at_least_one_gate_file_and_one_says_stop():
    gates = [p for p in _files() if re.match(r"^phase\d+_gate_", p.name)]
    assert gates, "no gate decision file in outputs/"
    stops = [p.name for p in gates if re.search(r"\bSTOP\b", p.read_text(encoding="utf-8"))]
    assert stops, "no gate file in outputs/ says STOP"


def test_log_keys_complete():
    data = json.loads((ROOT / "decision_log.json").read_text(encoding="utf-8"))
    assert isinstance(data, list) and data, "decision_log.json is empty"
    for i, entry in enumerate(data):
        missing = [k for k in REQUIRED_KEYS if k not in entry]
        assert not missing, f"entry #{i} ({entry.get('action')}) is missing {missing}"
        assert isinstance(entry["sources"], list)
        assert entry["model"] is None or isinstance(entry["model"], str)
        assert isinstance(entry["cost"], (int, float)) and not isinstance(entry["cost"], bool)
        assert entry["approved_by"] in ("founder", None)


def test_files_with_mocked_evidence_say_mocked():
    validator = _load_validator()
    entries = json.loads((ROOT / "decision_log.json").read_text(encoding="utf-8"))
    assert validator.check_mocked(entries, _files()) == []
