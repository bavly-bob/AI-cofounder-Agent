"""Check the repository against the submission conventions. Prints PASS/FAIL per item.

    python scripts/check_submission.py

Exit code 0 only when every item passes. Items that cannot pass yet because a
later milestone has not produced them are still FAIL, with a note saying what
is expected and when ("pending"). Nothing is hidden. Standard library only.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

README_HEADINGS = [
    "Quickstart", "What works and what is mocked", "Architecture", "Models and free resources", "Cost per run",
    "Demo video", "What our system will NOT do", "How the project stays alive after the hackathon",
]
ARCH_HEADINGS = ["Overview", "Phases", "Gates", "Sale to cash", "Data flow", "Built vs. stubbed", "Roadmap"]
ARCH_PHASES = [
    "Phase 1: Discover", "Phase 2: Validate", "Phase 3: Build", "Phase 4: Commercial setup",
    "Phase 5: Launch (sell, deliver, get paid)", "Phase 6: Operate and learn",
]
OUTPUT_NAME = re.compile(r"^phase\d+_(?:step\d+|gate)_[a-z0-9_\-]+\.(?:md|json|csv|txt|html)$")
OUTPUT_EXTRA = {"payment_test_log.md", ".gitkeep"}
TEXT_EXT = {".md", ".json", ".csv", ".txt", ".html"}
MAX_OUTPUT_BYTES = 200_000
REQUIRED_LOG_KEYS = ("timestamp", "agent", "action", "sources", "model", "cost", "approved_by")
VIDEO_HOSTS = re.compile(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be|vimeo\.com|loom\.com|drive\.google\.com)/\S+")
# Text that speaks to whoever assesses the repository, or asks for a result.
ADDRESSED = re.compile(
    r"\b(?:dear|hello|hi|attention|note to|to the)\s+(?:judges?|reviewers?|graders?|evaluators?|jury|organi[sz]ers?)\b|"
    r"\b(?:judges?|graders?|jury|evaluators?)\s*(?:,|:|should|please|will|can|may)|"
    r"\bplease\s+(?:score|grade|rate|award|give)\b|\b(?:full|maximum|max|top|perfect)\s+(?:marks|points|score)\b|"
    r"\b(?:score|grade|rate)\s+(?:us|this (?:repo|project|submission|entry))\b|\bvote for us\b", re.I)
CLAUDE_MARKERS = ("hackathon submission conventions", "The organizers' checker finds each deliverable")
MIT_TEXT = """Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
associated documentation files (the "Software"), to deal in the Software without restriction, including without
limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the
Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all copies or substantial portions of
the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT
LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT
SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE."""
MERMAID_TYPES = ("flowchart", "graph", "sequenceDiagram", "stateDiagram", "stateDiagram-v2", "classDiagram",
                 "erDiagram", "gantt", "journey", "pie", "mindmap", "timeline", "gitGraph", "quadrantChart")
# A market-style number in an output line: $ amounts, percentages, big counts, market size.
CLAIM_NUMBER = re.compile(r"\$\s?\d|\b\d[\d,.]*\s?%|\b\d[\d,.]*\s?(?:k|m|bn|million|billion|thousand)\b|"
                          r"\b\d[\d,]{3,}\s+(?:users|customers|people|businesses|companies|founders|downloads|members)\b|"
                          r"\bmarket size\b|\bTAM\b|\bSAM\b", re.I)


class Item:
    def __init__(self, name: str, ok: bool, detail: str = "", pending: str | None = None):
        self.name, self.ok, self.detail, self.pending = name, ok, detail, pending


# --- helpers -------------------------------------------------------------------

def git(*args) -> tuple:
    try:
        p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        return p.returncode, p.stdout.strip()
    except OSError as e:
        return 1, str(e)


def tracked() -> list:
    code, out = git("ls-files")
    return out.splitlines() if code == 0 else []


def read(rel: str) -> str | None:
    path = ROOT / rel
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def strip_code(text: str) -> str:
    """Markdown without fenced code blocks (so '## x' inside a code block is not a heading)."""
    return re.sub(r"^(```|~~~).*?^\1[^\n]*$", "", text, flags=re.S | re.M)


def headings(text: str, level: int) -> list:
    marks = "#" * level
    return [m.group(1).strip() for m in re.finditer(rf"^{marks}[ \t]+(.+?)[ \t#]*$", strip_code(text), re.M)]


def section(text: str, heading: str) -> str:
    """Body of one '## heading' section (up to the next '## ')."""
    m = re.search(rf"^##[ \t]+{re.escape(heading)}[ \t]*$(.*?)(?=^##[ \t]|\Z)", text, re.S | re.M)
    return m.group(1) if m else ""


def tables(text: str) -> list:
    """Every markdown table as a list of rows (lists of cells), header first, separator dropped."""
    found, current = [], []
    for line in strip_code(text).splitlines() + [""]:
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                current.append(cells)
            continue
        if current:
            found.append(current)
            current = []
    return found


def norm_cell(cell: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`]", "", cell)).strip().lower()


def status_table(text: str) -> dict:
    """{component: status} from the first table in ``text`` that has a Status column."""
    for table in tables(text):
        header = [norm_cell(c) for c in table[0]]
        if "status" in header:
            col = header.index("status")
            return {norm_cell(r[0]): norm_cell(r[col]) for r in table[1:] if len(r) > col and r[0].strip()}
    return {}


# --- checks --------------------------------------------------------------------

def check_repo() -> list:
    items = []
    code, branch = git("rev-parse", "--abbrev-ref", "HEAD")
    items.append(Item("On the default branch main", code == 0 and branch == "main", f"current branch: {branch}"))
    files = tracked()
    envs = [f for f in files if Path(f).name == ".env" or (Path(f).name.startswith(".env.") and Path(f).name != ".env.example")]
    items.append(Item("No tracked .env file", not envs, ", ".join(envs) or "none tracked"))
    pm = [f for f in files if f.startswith("pm/")]
    items.append(Item("No tracked pm/ files", not pm, f"{len(pm)} tracked under pm/" if pm else "none tracked"))
    spec = importlib.util.spec_from_file_location("scan_secrets", ROOT / "scripts" / "scan_secrets.py")
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        findings = module.scan(ROOT)
        items.append(Item("No secrets in tracked files (scripts/scan_secrets.py)", not findings,
                          "; ".join(f"{p}:{n} {k}" for p, n, k in findings[:5]) or f"{len(files)} files scanned"))
        example = read(".env.example")
        if example is None:
            items.append(Item(".env.example exists with placeholders only", False, ".env.example is missing"))
        else:
            bad = module.check_env_example(example)
            items.append(Item(".env.example has placeholders only", not bad,
                              "; ".join(f"line {n}: {msg}" for n, msg in bad) or "no secret-like values"))
    except Exception as e:  # noqa: BLE001 - report, never crash
        items.append(Item("No secrets in tracked files (scripts/scan_secrets.py)", False, f"scanner failed: {e}"))
    return items


def check_readme() -> list:
    text = read("README.md")
    if text is None:
        return [Item("README.md exists", False, "missing")]
    items = []
    found = headings(text, 2)
    items.append(Item("README has exactly the 8 '##' headings, in order", found == README_HEADINGS,
                      "found: " + " | ".join(found)))
    quick = section(text, "Quickstart")
    blocks = re.findall(r"```bash\s*\n(.*?)```", quick, re.S)
    items.append(Item("Quickstart has a bash block with 'docker compose up'",
                      any(line.strip() == "docker compose up" for b in blocks for line in b.splitlines()),
                      f"{len(blocks)} bash block(s) in Quickstart"))
    cost = section(text, "Cost per run")
    items.append(Item("Cost per run contains a $ figure", bool(re.search(r"\$\s?\d", cost)),
                      cost.strip().splitlines()[0] if cost.strip() else "section empty"))
    video = section(text, "Demo video")
    has_link = bool(VIDEO_HOSTS.search(video))
    items.append(Item("Demo video has a YouTube/Vimeo/Loom/Drive link", has_link,
                      "link found" if has_link else "no video link yet",
                      pending=None if has_link else "expected until the CEO adds the link before the freeze"))
    rows = status_table(section(text, "What works and what is mocked"))
    bad = {k: v for k, v in rows.items() if not re.search(r"\b(?:working|mocked|stubbed)\b", v)}
    labelled = bool(rows) and not bad and any(re.search(r"mocked|stubbed", v) for v in rows.values())
    items.append(Item("README table: every step is Working / MOCKED / STUBBED, and mocks are labelled", labelled,
                      f"{len(rows)} rows" + (f"; unlabelled: {list(bad)[:3]}" if bad else "")))
    if rows:
        lowercase = [cell for table in tables(section(text, "What works and what is mocked")) for row in table[1:]
                     for cell in row[1:] if re.search(r"\b(?:mocked|stubbed)\b", cell, re.I)
                     and not re.search(r"MOCKED|STUBBED", cell)]
        items.append(Item("README table writes MOCKED / STUBBED in capitals", not lowercase,
                          "; ".join(lowercase[:3]) or "all in capitals"))
    return items


def check_addressed_text() -> Item:
    hits = []
    for rel in tracked():
        p = Path(rel)
        if rel == "CLAUDE.md" or p.parts[0] in ("tests", "evals", "pm", ".claude") or rel == "scripts/check_submission.py":
            continue
        if p.suffix.lower() not in {".md", ".py", ".txt", ".html", ".json", ".yml", ".yaml", ".toml", ""}:
            continue
        text = read(rel)
        if not text:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if ADDRESSED.search(line):
                hits.append(f"{rel}:{n}")
    return Item("No text addressed to reviewers or judges", not hits, ", ".join(hits[:8]) or "none found")


def check_license_and_compose() -> list:
    items = []
    text = read("LICENSE")
    if text is None:
        items.append(Item("LICENSE is the full MIT text", False, "LICENSE missing"))
    else:
        flat = lambda s: re.sub(r"\s+", " ", s).strip()
        ok = text.lstrip().startswith("MIT License") and flat(MIT_TEXT) in flat(text) \
            and bool(re.search(r"^Copyright \(c\) \d{4}", text, re.M | re.I))
        items.append(Item("LICENSE is the full canonical MIT text", ok, "matches" if ok else "text differs"))
    compose = [f for f in ("docker-compose.yml", "compose.yaml", "docker-compose.yaml", "compose.yml") if (ROOT / f).exists()]
    items.append(Item("docker-compose.yml (or compose.yaml) at the root", bool(compose), ", ".join(compose) or "missing"))
    return items


def check_log() -> list:
    text = read("decision_log.json")
    if text is None:
        return [Item("decision_log.json exists", False, "missing")]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return [Item("decision_log.json is valid JSON", False, str(e))]
    if not isinstance(data, list):
        return [Item("decision_log.json is a list", False, type(data).__name__)]
    if not data:
        return [Item("decision_log.json has the demo run's entries", False, "the list is empty",
                     pending="expected until the demo run is committed: run python scripts/demo_run.py (as founder), then python scripts/payment_test.py --demo")]
    problems = []
    for i, e in enumerate(data):
        if not isinstance(e, dict):
            problems.append(f"#{i} is not an object")
            continue
        missing = [k for k in REQUIRED_LOG_KEYS if k not in e]
        if missing:
            problems.append(f"#{i} misses {missing}")
            continue
        if not (isinstance(e["timestamp"], str) and isinstance(e["agent"], str) and isinstance(e["action"], str)
                and e["action"].strip()):
            problems.append(f"#{i} timestamp/agent/action must be strings")
        if not isinstance(e["sources"], list) or not all(isinstance(u, str) and re.match(r"https?://\S+$", u)
                                                          for u in e["sources"]):
            problems.append(f"#{i} sources must be a list of URLs")
        if e["model"] is not None and not isinstance(e["model"], str):
            problems.append(f"#{i} model must be a string or null")
        if isinstance(e["cost"], bool) or not isinstance(e["cost"], (int, float)) or e["cost"] < 0:
            problems.append(f"#{i} cost must be a number")
        if e["approved_by"] not in ("founder", None):
            problems.append(f"#{i} approved_by must be \"founder\" or null")
    return [Item("decision_log.json: a list; every entry has the 7 keys with the right types", not problems,
                 "; ".join(problems[:5]) or f"{len(data)} entries")]


def output_files() -> list:
    out = ROOT / "outputs"
    return sorted(p for p in out.iterdir() if p.is_file()) if out.is_dir() else []


def check_outputs() -> list:
    files = [p for p in output_files() if p.name != ".gitkeep"]
    pending = ("expected until the demo run is committed: run python scripts/demo_run.py (as founder), "
               "then python scripts/payment_test.py --demo")
    if not files:
        return [Item("outputs/ has the demo run's files", False, "outputs/ is empty", pending=pending)]
    items = []
    bad_names = [p.name for p in files if not (OUTPUT_NAME.match(p.name) or p.name in OUTPUT_EXTRA)]
    items.append(Item("outputs/ names match phase<N>_step<M>_<name> / phase<N>_gate_<name>", not bad_names,
                      ", ".join(bad_names[:5]) or f"{len(files)} files"))
    bad_type = [p.name for p in files if p.suffix.lower() not in TEXT_EXT or p.stat().st_size > MAX_OUTPUT_BYTES]
    items.append(Item(f"outputs/ files are text and small (<= {MAX_OUTPUT_BYTES // 1000} KB)", not bad_type,
                      ", ".join(bad_type[:5]) or "all text and small"))
    gates = [p for p in files if re.match(r"^phase\d+_gate_", p.name)]
    items.append(Item("outputs/ has at least one gate file", bool(gates), f"{len(gates)} gate file(s)",
                      pending=None if gates else pending))
    stops = [p.name for p in gates if re.search(r"\bSTOP\b", p.read_text(encoding="utf-8", errors="replace"))]
    items.append(Item("At least one gate decision says STOP", bool(stops), ", ".join(stops[:3]) or "no STOP found",
                      pending=None if gates else pending))
    unsourced = []
    for p in files:
        if p.suffix.lower() not in (".md", ".txt", ".html", ".csv") or p.name == "payment_test_log.md":
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for block in re.split(r"\n\s*\n", strip_code(text)):
            for n, line in enumerate(block.splitlines()):
                if line.startswith(">") or re.match(r"^\s*(?:\|?\s*)?(?:run_id|cost|total cost|tokens|target|actual)\b", line, re.I):
                    continue
                if CLAIM_NUMBER.search(line) and not re.search(r"https?://|ASSUMPTION|MOCKED|STUBBED", line) \
                        and not re.search(r"https?://|ASSUMPTION", block):
                    unsourced.append(f"{p.name}: {line.strip()[:70]}")
    items.append(Item("Market claims in outputs/ carry a URL or ASSUMPTION", not unsourced,
                      "; ".join(unsourced[:4]) or "every number-claim line is sourced or labelled"))
    mocked_unlabelled = []
    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\bmock(?:ed)?\b|\bstub(?:bed)?\b|status:\s*(?:mocked|stubbed)", text, re.I) \
                and not re.search(r"MOCKED|STUBBED", text):
            mocked_unlabelled.append(p.name)
    items.append(Item("Output files that involve mocks say MOCKED / STUBBED", not mocked_unlabelled,
                      ", ".join(mocked_unlabelled[:5]) or "labelled"))
    return items


def check_payment_log() -> Item:
    text = read("outputs/payment_test_log.md")
    if text is None:
        return Item("outputs/payment_test_log.md exists and starts with MOCKED", False, "missing",
                    pending="expected until the CEO runs python scripts/payment_test.py --demo (after scripts/demo_run.py)")
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    return Item("outputs/payment_test_log.md exists and starts with MOCKED", first.lstrip("# ").startswith("MOCKED"),
                f"first line: {first[:60]}")


def check_architecture() -> list:
    text = read("ARCHITECTURE.md")
    pending = "expected until M6 (docs-engineer writes ARCHITECTURE.md)"
    if text is None:
        return [Item("ARCHITECTURE.md exists", False, "missing", pending=pending)]
    placeholder = "TODO (M6)" in text
    p = pending if placeholder else None
    items = []
    found = headings(text, 2)
    items.append(Item("ARCHITECTURE has the 7 '##' headings, in order", found == ARCH_HEADINGS,
                      "found: " + " | ".join(found), pending=p if found != ARCH_HEADINGS else None))
    phases = headings(section(text, "Phases"), 3)
    items.append(Item("ARCHITECTURE 'Phases' has the 6 phase '###' headings, in order", phases == ARCH_PHASES,
                      "found: " + (" | ".join(phases) or "none"), pending=p))
    blocks = re.findall(r"```mermaid\s*\n(.*?)```", text, re.S)
    items.append(Item("ARCHITECTURE has >= 3 mermaid diagrams", len(blocks) >= 3, f"{len(blocks)} mermaid block(s)",
                      pending=p))
    bad = [b.strip().splitlines()[0] if b.strip() else "(empty)" for b in blocks
           if not b.strip() or b.strip().split()[0] not in MERMAID_TYPES]
    mmdc = shutil.which("mmdc")
    if mmdc and blocks:
        for b in blocks:
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "d.mmd"
                src.write_text(b, encoding="utf-8")
                r = subprocess.run([mmdc, "-i", str(src), "-o", str(Path(tmp) / "d.svg")], capture_output=True)
                if r.returncode != 0:
                    bad.append(b.strip().splitlines()[0])
    items.append(Item("Mermaid blocks are valid" + (" (mermaid-cli)" if mmdc else " (diagram type check; mermaid-cli not installed)"),
                      bool(blocks) and not bad, "; ".join(bad[:3]) or f"{len(blocks)} checked",
                      pending=p if not blocks else None))
    readme_rows = status_table(section(read("README.md") or "", "What works and what is mocked"))
    arch_rows = status_table(section(text, "Built vs. stubbed"))
    same = bool(readme_rows) and readme_rows == arch_rows
    diff = sorted(set(readme_rows.items()) ^ set(arch_rows.items()))
    items.append(Item("README 'What works' table equals the ARCHITECTURE 'Built vs. stubbed' table", same,
                      f"{len(diff)} differing row(s): " + "; ".join(f"{k} = {v}" for k, v in diff[:4]) if diff else "identical",
                      pending=p))
    return items


PATH_TOKEN = re.compile(r"`([A-Za-z0-9_.\-/]+\.(?:py|md|json|yml|yaml|txt|csv|html|toml|ts|js|example)|"
                        r"[A-Za-z0-9_.\-]+/(?:[A-Za-z0-9_.\-]+/?)*)`")


def check_paths_and_links() -> list:
    items, missing, broken = [], [], []
    files = set(tracked())
    dirs = {str(Path(f).parent.as_posix()) for f in files} | {"/".join(Path(f).parts[:i]) for f in files
                                                              for i in range(1, len(Path(f).parts))}
    docs = ["README.md", "ARCHITECTURE.md"] + sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "docs").rglob("*.md"))
    for rel in ("README.md", "ARCHITECTURE.md"):
        text = read(rel) or ""
        for token in PATH_TOKEN.findall(text):
            t = token.rstrip("/")
            if not t or t.startswith(("http", "runs/", ".env", "~", "/")) or any(c in t for c in "<>*{}$") \
                    or t in (".", ".."):
                continue
            if "/" not in t and not (ROOT / t).exists() and t not in files:
                # a bare file name: accept it if any tracked file has that name
                if not any(Path(f).name == t for f in files):
                    missing.append(f"{rel}: {t}")
                continue
            if "/" in t and t not in files and t not in dirs and not (ROOT / t).exists():
                missing.append(f"{rel}: {t}")
    items.append(Item("Every file path mentioned in README/ARCHITECTURE exists", not missing,
                      "; ".join(missing[:6]) or "all found"))
    for rel in docs:
        text = read(rel) or ""
        base = (ROOT / rel).parent
        for target in re.findall(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", strip_code(text)):
            if re.match(r"^(?:https?|mailto):", target) or target.startswith("#"):
                continue
            path = target.split("#", 1)[0]
            if path and not (base / path).exists():
                broken.append(f"{rel}: {target}")
    items.append(Item("Relative links in README/ARCHITECTURE/docs resolve", not broken,
                      "; ".join(broken[:6]) or f"{len(docs)} file(s) checked"))
    return items


def check_docs_folder() -> Item:
    bad = []
    docs = ROOT / "docs"
    if docs.is_dir():
        for p in docs.rglob("*"):
            if not p.is_file():
                continue
            name = p.name.lower()
            text = p.read_text(encoding="utf-8", errors="replace") if p.suffix.lower() in TEXT_EXT else ""
            if name == "claude.md" or "submission_guide" in name or "submission-guide" in name \
                    or any(m in text for m in CLAUDE_MARKERS):
                bad.append(p.relative_to(ROOT).as_posix())
    return Item("CLAUDE.md and the organizers' guide are not in docs/", not bad, ", ".join(bad) or "not present")


def check_pytest() -> Item:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as e:
        return Item("pytest -q passes", False, f"could not run pytest: {e}")
    lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
    return Item("pytest -q passes", r.returncode == 0, lines[-1] if lines else f"exit code {r.returncode}")


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    items = []
    for group in (check_repo, check_readme, lambda: [check_addressed_text()], check_license_and_compose, check_log,
                  check_outputs, lambda: [check_payment_log()], check_architecture, check_paths_and_links,
                  lambda: [check_docs_folder()], lambda: [check_pytest()]):
        try:
            items += group()
        except Exception as e:  # noqa: BLE001 - one broken check must not hide the others
            items.append(Item(getattr(group, "__name__", "check"), False, f"check crashed: {type(e).__name__}: {e}"))
    fails = 0
    pending = 0
    for item in items:
        if item.ok:
            print(f"PASS  {item.name}" + (f"  ({item.detail})" if item.detail else ""))
            continue
        fails += 1
        note = ""
        if item.pending:
            pending += 1
            note = f"  [pending: {item.pending}]"
        print(f"FAIL  {item.name}" + (f"  ({item.detail})" if item.detail else "") + note)
    print()
    print(f"{len(items) - fails} PASS, {fails} FAIL ({pending} of the FAILs are marked pending, "
          f"{fails - pending} are not)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
