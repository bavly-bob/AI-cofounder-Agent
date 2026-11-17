import json
import re
from pathlib import Path
from unittest import mock

import requests

import llm

ROOT = Path(__file__).resolve().parent.parent
HEADINGS = [
    "## Quickstart",
    "## What works and what is mocked",
    "## Architecture",
    "## Models and free resources",
    "## Cost per run",
    "## Demo video",
    "## What our system will NOT do",
    "## How the project stays alive after the hackathon",
]


def readme():
    return (ROOT / "README.md").read_text(encoding="utf-8")


def test_required_root_files_exist():
    for name in ["README.md", "LICENSE", "docker-compose.yml", "decision_log.json", "ARCHITECTURE.md"]:
        assert (ROOT / name).is_file(), name
    for name in ["outputs", "tests"]:
        assert (ROOT / name).is_dir(), name


def test_readme_headings_in_order():
    found = [line.strip() for line in readme().splitlines() if line.startswith("## ")]
    assert found == HEADINGS


def test_quickstart_has_docker_compose_up():
    quickstart = readme().split("## Quickstart", 1)[1].split("\n## ", 1)[0]
    assert "```bash\ndocker compose up\n```" in quickstart


def test_decision_log_is_list():
    assert isinstance(json.loads((ROOT / "decision_log.json").read_text(encoding="utf-8")), list)


def test_env_example_has_no_secrets():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert not re.search(r"(sk-|sk_live|pk_live|ghp_|AKIA)[A-Za-z0-9]", text)
    assert not re.search(r"(KEY|SECRET|TOKEN|PASSWORD)\s*=\s*\S", text, re.I)


def test_generate_unreachable_returns_error():
    with mock.patch("llm.requests.post", side_effect=requests.ConnectionError("refused")):
        result = llm.generate("hi")
    assert result["text"] == "" and result["cost"] == 0 and result["model"]
    assert "Ollama" in result["error"]
