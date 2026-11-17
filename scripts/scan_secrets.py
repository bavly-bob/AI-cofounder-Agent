"""Scan every git-tracked file for secrets. Exit 1 if anything is found.

    python scripts/scan_secrets.py

Looks for: a tracked .env file, private keys, provider API keys and tokens,
live payment keys, hard-coded password/secret assignments, and real values in
.env.example (placeholders only). Standard library only.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_BYTES = 2_000_000

PATTERNS = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("OpenAI/Anthropic-style key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}")),
    ("live payment key", re.compile(r"\b(?:sk|pk|rk)_live_[A-Za-z0-9]{10,}")),
    ("payment test key", re.compile(r"\b(?:sk|rk)_test_[A-Za-z0-9]{10,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{15,}\.eyJ[A-Za-z0-9_\-]{15,}\.[A-Za-z0-9_\-]{10,}")),
    ("hard-coded secret", re.compile(
        r"""(?i)\b(?:api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token|password|passwd)\b"""
        r"""\s*[:=]\s*['"]([^'"\s]{8,})['"]""")),
]
# Values that are obviously placeholders, not secrets.
PLACEHOLDER = re.compile(r"(?i)^(?:|x+|\*+|changeme|change-me|placeholder|your[_\-a-z]*|<[^>]*>|\$\{[^}]*\}|example|dummy|test|none|null|"
                         r"replace[_\-a-z]*|todo|fake[_\-a-z0-9]*|redacted|\.\.\.)$")
SECRET_NAME = re.compile(r"(?i)(?:key|secret|token|password|passwd|credential|private)")


def tracked_files(root: Path = ROOT) -> list:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        raise SystemExit(f"Could not list tracked files with git: {e}")
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def check_env_example(text: str) -> list:
    problems = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        value = value.strip("'\"")
        if SECRET_NAME.search(name) and not PLACEHOLDER.match(value):
            problems.append((n, f"{name} has a value that does not look like a placeholder"))
    return problems


def scan_text(rel: str, text: str) -> list:
    """[(line, kind)] for one file's text."""
    findings = []
    for n, line in enumerate(text.splitlines(), 1):
        for kind, rx in PATTERNS:
            m = rx.search(line)
            if not m:
                continue
            if kind == "hard-coded secret" and PLACEHOLDER.match(m.group(1)):
                continue
            findings.append((n, kind))
    if Path(rel).name == ".env.example":
        findings += check_env_example(text)
    return findings


def scan(root: Path = ROOT) -> list:
    """[(path, line, kind)] for every finding in tracked files."""
    findings = []
    for rel in tracked_files(root):
        name = Path(rel).name
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            findings.append((rel, 0, "tracked environment file (must be untracked)"))
            continue
        path = root / rel
        try:
            if not path.is_file() or path.stat().st_size > MAX_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue  # binary
        for line, kind in scan_text(rel, data.decode("utf-8", "replace")):
            findings.append((rel, line, kind))
    return findings


def main() -> int:
    findings = scan()
    if not findings:
        print(f"PASS  no secrets found in {len(tracked_files())} tracked files")
        return 0
    for rel, line, kind in findings:
        print(f"FAIL  {rel}:{line}  {kind}")   # the value itself is never printed
    print(f"{len(findings)} possible secret(s) found")
    return 1


if __name__ == "__main__":
    sys.exit(main())
