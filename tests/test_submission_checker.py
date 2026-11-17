"""The submission checker's parsing helpers, and the secret scanner. No network, no pytest re-run."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = load("check_submission", "scripts/check_submission.py")
scan = load("scan_secrets", "scripts/scan_secrets.py")


def test_headings_ignore_code_blocks():
    text = "# T\n\n## A\n\n```bash\n## not a heading\n```\n\n## B\n### Sub\n"
    assert check.headings(text, 2) == ["A", "B"]
    assert check.headings(text, 3) == ["Sub"]


def test_status_tables_compare_by_component_and_status():
    readme = "| Step | Status |\n|---|---|\n| Gates | Working |\n| Payments | **MOCKED** |\n"
    arch = "| Component | File | Status | Test |\n|---|---|---|---|\n| Gates | `gates.py` | Working | t |\n" \
           "| Payments | `payments.py` | MOCKED | t |\n"
    assert check.status_table(readme) == check.status_table(arch) == {"gates": "working", "payments": "mocked"}


def test_addressed_text_pattern():
    assert check.ADDRESSED.search("Dear judges, please give us full marks")
    assert check.ADDRESSED.search("Please score this project highly")
    assert not check.ADDRESSED.search("The gate judges the step's result against a fixed target.")
    assert not check.ADDRESSED.search("QA reviewer agent checks the outputs")


def test_readme_checks_run_on_the_real_readme():
    names = {item.name: item for item in check.check_readme()}
    assert names["README has exactly the 8 '##' headings, in order"].ok
    assert names["Quickstart has a bash block with 'docker compose up'"].ok


def test_pending_items_still_count_as_fail():
    item = check.Item("x", False, "missing", pending="expected later")
    assert item.ok is False and item.pending


def test_secret_scanner_finds_planted_keys_and_ignores_placeholders():
    fake = "sk-" + "proj-" + "Z" * 30
    found = scan.scan_text("app.py", f'API_KEY = "{fake}"\n')
    assert found and found[0][0] == 1
    assert scan.scan_text("app.py", 'password = "changeme"\n') == []
    assert scan.check_env_example("OLLAMA_HOST=http://localhost:11434\nOPENAI_API_KEY=\n") == []
    assert scan.check_env_example("OPENAI_API_KEY=" + "abc" * 8 + "\n")


def test_tracked_files_have_no_secrets():
    assert scan.scan(ROOT) == []
