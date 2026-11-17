"""Checks the committed demo run in ``outputs/`` and the root ``decision_log.json``.

    python scripts/validate_demo.py

Checks:
- every log entry has the seven required keys with the right types;
- every gated action (``approval_request``) has a founder approval
  (``approved_by: "founder"``) or a recorded rejection/denial, and every executed
  action and gate confirmation/override carries ``approved_by: "founder"``;
- every URL in an ``outputs/`` file appears in some log entry's ``sources``;
- output file names match ``phase<N>_(step<M>|gate)_<name>.(md|json|html)``
  (``payment_test_log.md`` is the one named exception), files are text and
  under 100 KB, and the log is under 300 KB;
- at least one gate file exists and at least one says STOP;
- files built from MOCKED evidence say "MOCKED".

Exits 1 with the list of failures, 0 when everything passes. Reads only.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

NAME_RE = re.compile(r"^phase\d+_(step\d+|gate)_[a-z0-9_]+\.(md|json|html)$")
EXTRA_NAMES = {"payment_test_log.md", ".gitkeep"}
TEXT_SUFFIXES = {".md", ".json", ".html"}
MAX_FILE_BYTES = 100_000
MAX_LOG_BYTES = 300_000
REQUIRED_KEYS = ("timestamp", "agent", "action", "sources", "model", "cost", "approved_by")
URL_RE = re.compile(r"https?://[^\s<>\"'`)\]|]+")
MOCK_WORD_RE = re.compile(r"\bmock(?:ed|s)?\b", re.I)
STUB_WORD_RE = re.compile(r"\bstub(?:bed|s)?\b", re.I)
GATE_FILE_RE = re.compile(r"^phase\d+_gate_")
FOUNDER = "founder"


def is_url(value) -> bool:
    return isinstance(value, str) and bool(re.match(r"^https?://[^\s/]+\S*$", value))


def clean_url(url: str) -> str:
    return url.rstrip(".,;:!?*_")


# --- log ------------------------------------------------------------------------

def load_log(path: Path) -> tuple:
    """(entries, problems)."""
    if not path.is_file():
        return [], [f"{path.name} is missing"]
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "[]")
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return [], [f"{path.name} is not valid JSON: {e}"]
    if not isinstance(data, list):
        return [], [f"{path.name} must hold a JSON list"]
    if not data:
        return [], [f"{path.name} is empty: the demo run has not been committed"]
    return data, []


def check_entries(entries: list) -> list:
    problems = []
    for i, e in enumerate(entries):
        where = f"log entry #{i}"
        if not isinstance(e, dict):
            problems.append(f"{where} is not an object")
            continue
        where += f" ({e.get('agent')}/{e.get('action')})"
        missing = [k for k in REQUIRED_KEYS if k not in e]
        if missing:
            problems.append(f"{where} is missing {missing}")
            continue
        for key in ("timestamp", "agent", "action"):
            if not isinstance(e[key], str) or not e[key].strip():
                problems.append(f"{where}: '{key}' must be a non-empty string")
        if not isinstance(e["sources"], list) or not all(is_url(u) for u in e["sources"]):
            problems.append(f"{where}: 'sources' must be a list of http(s) URLs")
        if e["model"] is not None and not isinstance(e["model"], str):
            problems.append(f"{where}: 'model' must be a string or null")
        cost = e["cost"]
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
            problems.append(f"{where}: 'cost' must be a non-negative number")
        if e["approved_by"] not in (FOUNDER, None):
            problems.append(f"{where}: 'approved_by' must be \"founder\" or null")
    return problems


def check_gated_actions(entries: list) -> tuple:
    """(problems, warnings). Every approval request is answered by the founder or denied, on record."""
    problems, warnings = [], []
    open_requests = {}        # (run_id, kind, summary) -> [index, ...]
    granted = {}              # (run_id, kind, summary) -> count of approvals not yet executed
    pending_only = []         # (index, key): answered "awaiting"; needs a later founder decision
    decided = set()           # (run_id, kind, summary) with a founder_decision entry (browser path)
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        action, key = e.get("action"), (e.get("run_id"), e.get("kind"), e.get("summary"))
        label = f"log entry #{i} ({e.get('agent')}/{action}, kind {e.get('kind')})"
        if action == "approval_request":
            open_requests.setdefault(key, []).append(i)
        elif action in ("approval_granted", "approval_denied", "approval_pending"):
            if not open_requests.get(key):
                problems.append(f"{label} has no matching approval_request before it")
            else:
                req = open_requests[key].pop(0)
                if action == "approval_pending":
                    pending_only.append((req, key))
            if action == "approval_granted":
                if e.get("approved_by") != FOUNDER:
                    problems.append(f"{label}: an approval must have approved_by \"founder\"")
                granted[key] = granted.get(key, 0) + 1
            elif action == "approval_denied":
                if e.get("approved_by") is not None:
                    problems.append(f"{label}: a denial must have approved_by null")
                if e.get("decision") not in ("rejected", "denied_by_default"):
                    problems.append(f"{label}: a denial must record decision rejected/denied_by_default")
        elif action == "founder_decision":
            decided.add(key)
            if e.get("approved_by") != FOUNDER and e.get("decision") != "rejected":
                problems.append(f"{label}: a founder decision is approved_by \"founder\" or decision \"rejected\"")
        elif isinstance(action, str) and action.startswith("execute_") and e.get("agent") == "approval":
            if e.get("approved_by") != FOUNDER:
                problems.append(f"{label}: an executed action must have approved_by \"founder\"")
            if granted.get(key, 0) < 1:
                problems.append(f"{label}: executed without an approval_granted before it")
            else:
                granted[key] -= 1
        elif e.get("agent") == "gate" and action in ("founder_confirm", "founder_override"):
            if e.get("approved_by") != FOUNDER:
                problems.append(f"{label}: a founder gate decision must have approved_by \"founder\"")
        elif e.get("agent") == "gate" and action == "founder_review_none":
            warnings.append(f"log entry #{i}: gate '{e.get('gate')}' (run {e.get('run_id')}) had no founder "
                            "decision; the system recommendation stood")
    for key, indexes in open_requests.items():
        for req in indexes:
            problems.append(f"log entry #{req}: approval_request (kind {key[1]}, run {key[0]}) has no founder "
                            "approval or recorded rejection/denial")
    for req, key in pending_only:
        if key not in decided:
            problems.append(f"log entry #{req}: approval_request (kind {key[1]}, run {key[0]}) was left awaiting "
                            "the founder, with no decision recorded")
    return problems, warnings


def log_sources(entries: list) -> set:
    urls = set()
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("sources"), list):
            urls.update(u for u in e["sources"] if isinstance(u, str))
    return urls


# --- outputs ----------------------------------------------------------------------

def output_files(outputs_dir: Path) -> list:
    if not outputs_dir.is_dir():
        return []
    return sorted(p for p in outputs_dir.iterdir() if p.is_file() and p.name != ".gitkeep")


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def check_files(files: list) -> list:
    problems = []
    for p in files:
        if not (NAME_RE.match(p.name) or p.name in EXTRA_NAMES):
            problems.append(f"outputs/{p.name}: the name does not match "
                            "phase<N>_(step<M>|gate)_<name>.(md|json|html)")
        if p.suffix.lower() not in TEXT_SUFFIXES:
            problems.append(f"outputs/{p.name}: not a text format (.md, .json, .html)")
        size = p.stat().st_size
        if size >= MAX_FILE_BYTES:
            problems.append(f"outputs/{p.name}: {size} bytes, the limit is {MAX_FILE_BYTES}")
        if read_text(p) is None:
            problems.append(f"outputs/{p.name}: not readable as UTF-8 text")
    return problems


def urls_in(text: str) -> list:
    return [clean_url(u) for u in URL_RE.findall(text or "")]


def check_urls(files: list, entries: list) -> list:
    known = log_sources(entries)
    known_loose = {u.rstrip("/") for u in known}
    problems = []
    for p in files:
        for url in sorted(set(urls_in(read_text(p) or ""))):
            if url not in known and url.rstrip("/") not in known_loose:
                problems.append(f"outputs/{p.name}: {url} is not in any log entry's sources")
    return problems


def check_gates(files: list) -> list:
    gates = [p for p in files if GATE_FILE_RE.match(p.name)]
    if not gates:
        return ["outputs/ has no gate decision file (phase<N>_gate_<name>.md)"]
    if not any(re.search(r"\bSTOP\b", read_text(p) or "") for p in gates):
        return ["no gate file in outputs/ says STOP"]
    return []


def mocked_files(entries: list, files: list) -> dict:
    """{file name: label it must carry} for files built from MOCKED evidence or STUBBED steps."""
    names = {p.name for p in files}
    need = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        # demo_run.py records which committed files rest on MOCKED evidence.
        if e.get("action") == "merge_run_log" and e.get("mock_evidence_phases"):
            phases = {int(x) for x in e["mock_evidence_phases"] if isinstance(x, int)}
            for name in e.get("files") or []:
                m = re.match(r"^phase(\d+)_", str(name))
                if m and int(m.group(1)) in phases and name in names:
                    need[name] = "MOCKED"
        # A step or gate whose own status/basis is MOCKED or STUBBED.
        label = next((str(e.get(k)).upper() for k in ("status", "basis")
                      if str(e.get(k)).upper() in ("MOCKED", "STUBBED")), None)
        if e.get("mocked") is True:
            label = "MOCKED"
        if label:
            for key in ("output_file_committed", "output_file"):
                name = e.get(key)
                if isinstance(name, str) and name in names:
                    need.setdefault(name, label)
    # Any file that talks about mocks or stubs must use the label.
    for p in files:
        text = read_text(p) or ""
        if MOCK_WORD_RE.search(text):
            need.setdefault(p.name, "MOCKED")
        elif STUB_WORD_RE.search(text):
            need.setdefault(p.name, "STUBBED")
    return need


def check_mocked(entries: list, files: list) -> list:
    by_name = {p.name: p for p in files}
    problems = []
    for name, label in sorted(mocked_files(entries, files).items()):
        text = read_text(by_name[name]) or ""
        ok = "MOCKED" in text if label == "MOCKED" else bool(re.search(r"MOCKED|STUBBED", text))
        if not ok:
            problems.append(f"outputs/{name}: built from {label.lower()} input but does not say {label}")
    return problems


def validate(root: Path = ROOT) -> tuple:
    """(problems, warnings, facts)."""
    root = Path(root)
    log_path = root / "decision_log.json"
    entries, problems = load_log(log_path)
    warnings = []
    if log_path.is_file() and log_path.stat().st_size >= MAX_LOG_BYTES:
        problems.append(f"decision_log.json is {log_path.stat().st_size} bytes, the limit is {MAX_LOG_BYTES}")
    files = output_files(root / "outputs")
    if not files:
        problems.append("outputs/ has no files: the demo run has not been committed")
    problems += check_entries(entries)
    gated, warned = check_gated_actions(entries)
    problems += gated
    warnings += warned
    problems += check_files(files)
    problems += check_urls(files, entries)
    if files:
        problems += check_gates(files)
    problems += check_mocked(entries, files)
    facts = {"entries": len(entries), "files": len(files),
             "runs": sorted({e.get("run_id") for e in entries if isinstance(e, dict) and e.get("run_id")})}
    return problems, warnings, facts


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Validate the committed demo run.")
    parser.add_argument("--root", default=str(ROOT), help="repository root (default: this repository)")
    args = parser.parse_args(argv)
    problems, warnings, facts = validate(Path(args.root))
    print(f"decision_log.json: {facts['entries']} entries; outputs/: {facts['files']} files; "
          f"run ids: {', '.join(facts['runs']) or 'none'}")
    for w in warnings:
        print(f"WARNING: {w}")
    if problems:
        print(f"\nFAIL: {len(problems)} problem(s)")
        for p in problems:
            print(f"- {p}")
        return 1
    print("PASS: the demo run is complete and consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
