"""The committed demo run. A human (a team member acting as the founder) answers every prompt.

    python scripts/demo_run.py          # both demos, full Phase 1 research budget
    python scripts/demo_run.py --fast   # both demos, smaller research budget

What it does, in order:
1. Pre-flight (nothing is written yet): an interactive terminal, Ollama reachable,
   the model pulled, the orchestrator supports the CLI flags the demos need, the
   briefs exist. Any failure exits with a friendly message.
2. Asks once whether to start, because it resets the root ``decision_log.json``
   and replaces earlier demo files in ``outputs/``.
3. Demo A: ``examples/briefs/weak_idea.json``, Phase 1 only (expected: STOP).
   Demo B: ``examples/briefs/decent_idea.json``, Phases 1-2 with MOCKED evidence
   ``--mock-signups 12 --mock-visitors 400`` (expected: Phase 1 GO, then Phase 2 STOP/PIVOT).
   Each demo runs ``orchestrator.py`` as a normal CLI run (real local model, real
   web, ``CLIApprover``) into its own ``runs/<run_id>/``. The founder answers each
   gate and approval prompt in this terminal. There is no flag that answers for them.
4. After each demo: the run's output files are copied to ``outputs/`` with a
   ``_weak`` / ``_decent`` suffix (content unchanged), and the run's log entries are
   appended to the root ``decision_log.json``, tagged by ``run_id`` and demo.
   Long text fields are trimmed so the log stays under 300 KB; every entry is kept.
5. Writes ``outputs/phase0_step0_demo_summary.md``: the runs, the briefs, the
   outcomes as they came out, the model, the wall time, the tokens and what was MOCKED.

Why not ``orchestrator.py --demo``: a ``--demo`` run resets the root log, so the
second demo would erase the first. This script resets the log once and appends.
Run ``scripts/payment_test.py --demo`` AFTER this script (it appends to the log).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NAME_RE = re.compile(r"^phase\d+_(step\d+|gate)_[a-z0-9_]+\.(md|json|html)$")
MAX_FILE_BYTES = 100_000
MAX_LOG_BYTES = 300_000
SUMMARY_NAME = "phase0_step0_demo_summary.md"
REQUIRED_KEYS = ("timestamp", "agent", "action", "sources", "model", "cost", "approved_by")
TRIM_LEVELS = (1000, 400, 150, 60, 20)   # max characters per extra text field, tried in turn
MAX_LIST_ITEMS = (50, 20, 10, 5, 3)
GATE_META_ACTIONS = {"set_phase_thresholds", "set_gate_target", "founder_confirm", "founder_override",
                     "founder_review_none"}
FOUNDER_GATE_ACTIONS = {"founder_confirm", "founder_override", "founder_review_none"}


@dataclass
class Demo:
    key: str                      # "A" / "B"
    label: str                    # file suffix: "weak" / "decent"
    brief: str                    # path relative to the repo root
    phases: str | None = None     # e.g. "1" or "1,2"; always set (the CLI default is all phases)
    mock_signups: int | None = None
    mock_visitors: int | None = None
    expected: str = ""

    @property
    def tag(self) -> str:
        return f"{self.key}_{self.label}"

    @property
    def mocked(self) -> bool:
        return self.mock_signups is not None or self.mock_visitors is not None

    @property
    def mock_evidence_phases(self) -> list:
        """Phases whose files are built from MOCKED evidence (Phase 2 sign-up numbers)."""
        return [2] if self.mocked else []

    def cli_args(self, fast: bool = False) -> list:
        args = ["--brief", self.brief]
        if self.phases:
            args += ["--phases", self.phases]
        if self.mock_signups is not None:
            args += ["--mock-signups", str(self.mock_signups)]
        if self.mock_visitors is not None:
            args += ["--mock-visitors", str(self.mock_visitors)]
        if fast:
            args.append("--fast")
        return args

    def required_flags(self) -> list:
        flags = []
        if self.phases:
            flags.append("--phases")
        if self.mock_signups is not None:
            flags.append("--mock-signups")
        if self.mock_visitors is not None:
            flags.append("--mock-visitors")
        return flags


DEMOS = [
    Demo("A", "weak", "examples/briefs/weak_idea.json", phases="1",
         expected="Phase 1 ends in STOP (weak evidence)."),
    Demo("B", "decent", "examples/briefs/decent_idea.json", phases="1,2", mock_signups=12, mock_visitors=400,
         expected="Phase 1 GO, then Phase 2 STOP or PIVOT on MOCKED evidence (12 sign-ups / 400 visitors)."),
]


@dataclass
class DemoResult:
    demo: Demo
    run_id: str | None = None
    returncode: int | None = None
    wall_seconds: float = 0.0
    files: list = field(default_factory=list)       # committed names in outputs/
    skipped: list = field(default_factory=list)     # (name, reason) not copied
    entries: int = 0
    status: str | None = None
    outcome: str | None = None
    tokens: int = 0
    cost: float = 0
    model: str | None = None
    gates: list = field(default_factory=list)       # dicts: where, gate, system, founder, final, basis
    founder_actions: list = field(default_factory=list)
    problems: list = field(default_factory=list)


# --- pre-flight -----------------------------------------------------------------

def ollama_settings() -> tuple:
    """(host, model), read exactly as the app's model client reads them."""
    import llm
    return llm._settings()


def check_ollama(host: str, model: str, status=None) -> tuple:
    """(ok, message). Checks that Ollama answers and that ``model`` is pulled.

    ``status`` returns {"state": "ready" | "loading" | "offline"}; by default the app's own
    read-only check (``ui/ollama_status.py``: one GET of Ollama's /api/tags).
    """
    if status is None:
        from ui import ollama_status
        status = ollama_status.check
    try:
        state = (status() or {}).get("state")
    except Exception:
        state = "offline"
    if state == "ready":
        return True, f"Ollama is reachable at {host} and '{model}' is pulled."
    if state == "loading":
        return False, (f"Ollama is running at {host}, but the model '{model}' is not pulled yet.\n"
                       f"  Run: ollama pull {model}\n  Then run this script again.")
    return False, (f"Ollama is not reachable at {host}.\n"
                   "  Start it first, then run this script again:\n"
                   "    - a local Ollama install: open the Ollama app, or run `ollama serve`\n"
                   "    - or Docker, with port 11434 published to this machine\n"
                   "  If Ollama runs somewhere else, set OLLAMA_HOST in .env.")


def orchestrator_help(root: Path = ROOT) -> str:
    """The orchestrator CLI's --help text (argparse prints it and exits)."""
    buf = io.StringIO()
    try:
        import orchestrator
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            orchestrator.main(["--help"])
    except SystemExit:
        pass
    except Exception as e:
        return f"(could not read orchestrator.py --help: {e})"
    return buf.getvalue()


def missing_flags(help_text: str, demos) -> list:
    """The CLI flags the demos need that ``orchestrator.py --help`` does not list."""
    needed = []
    for demo in demos:
        for flag in demo.required_flags():
            if flag not in needed:
                needed.append(flag)
    return [f for f in needed if not re.search(re.escape(f) + r"(?![\w-])", help_text)]


def preflight(root: Path, demos, stdin_is_tty: bool, host: str, model: str, status=None,
              help_text: str | None = None) -> list:
    """Every reason not to start. Empty list = ready. Writes nothing."""
    problems = []
    if not stdin_is_tty:
        problems.append("This script must run in an interactive terminal: the founder answers every prompt "
                        "by typing. Piped or redirected input is not accepted. On Windows use PowerShell or "
                        "Command Prompt (Git Bash's mintty window is not seen as interactive).")
    for demo in demos:
        if not (root / demo.brief).is_file():
            problems.append(f"Demo {demo.key}: the brief {demo.brief} is missing.")
    ok, message = check_ollama(host, model, status)
    if not ok:
        problems.append(message)
    text = help_text if help_text is not None else orchestrator_help(root)
    missing = missing_flags(text, demos)
    if missing:
        problems.append("orchestrator.py does not support these flags yet: " + ", ".join(missing) +
                        ". Demo B needs Phase 2 (milestone M3: --phases 1,2 --mock-signups N "
                        "--mock-visitors V). Run the demo after M3 has landed.")
    return problems


# --- files ------------------------------------------------------------------------

def suffixed_name(name: str, label: str) -> str:
    """phase1_gate_discover.md + weak -> phase1_gate_discover_weak.md"""
    stem, dot, ext = name.rpartition(".")
    return f"{stem}_{label}.{ext}" if dot else f"{name}_{label}"


def collect_run_files(run_dir: Path, label: str) -> tuple:
    """([(source path, committed name)], [(name, reason)]) for the run's top-level output files."""
    pairs, skipped = [], []
    for path in sorted(p for p in Path(run_dir).iterdir() if p.is_file()):
        if not path.name.startswith("phase"):
            continue   # decision_log.json, state.json, ...
        new = suffixed_name(path.name, label)
        if not NAME_RE.match(new):
            skipped.append((path.name, "the name does not match the output naming rule"))
            continue
        size = path.stat().st_size
        if size >= MAX_FILE_BYTES:
            skipped.append((path.name, f"{size} bytes is over the {MAX_FILE_BYTES} byte limit"))
            continue
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped.append((path.name, "not UTF-8 text"))
            continue
        pairs.append((path, new))
    return pairs, skipped


def copy_run_files(pairs, outputs_dir: Path) -> list:
    """Byte-for-byte copies (content is never changed). Returns the committed names."""
    outputs_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for src, new in pairs:
        shutil.copyfile(src, outputs_dir / new)
        names.append(new)
    return names


def clean_previous_demo_files(outputs_dir: Path, labels) -> list:
    """Remove files an earlier demo_run left in outputs/ (only our own suffixes and the summary)."""
    if not outputs_dir.is_dir():
        return []
    removed = []
    for path in outputs_dir.iterdir():
        if not path.is_file():
            continue
        ours = path.name == SUMMARY_NAME or any(
            NAME_RE.match(path.name) and path.stem.endswith(f"_{label}") for label in labels)
        if ours:
            path.unlink()
            removed.append(path.name)
    return sorted(removed)


def find_new_run_dir(runs_dir: Path, before: set) -> tuple:
    """(run dir or None, warning). The folder the orchestrator created during this demo."""
    if not runs_dir.is_dir():
        return None, f"The orchestrator created no new run folder ({runs_dir} does not exist)."
    new = [p for p in runs_dir.iterdir() if p.is_dir() and p.name not in before
           and (p / "decision_log.json").is_file()]
    if not new:
        return None, "The orchestrator created no new run folder with a decision_log.json."
    new.sort(key=lambda p: (p / "decision_log.json").stat().st_mtime)
    warning = "" if len(new) == 1 else (f"{len(new)} new run folders appeared; using the newest, {new[-1].name}. "
                                        "Do not use the app while the demo runs.")
    return new[-1], warning


# --- log ----------------------------------------------------------------------------

def read_log(path: Path) -> list:
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8") or "[]")
    if not isinstance(data, list):
        raise ValueError(f"{path} must hold a JSON list")
    return data


def write_log(path: Path, entries: list) -> None:
    """Validate every entry, then replace the file atomically."""
    from decision_log import validate_entry
    for entry in entries:
        validate_entry(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".decision_log_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def log_size(entries: list) -> int:
    return len((json.dumps(entries, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _trim(value, limit: int, max_items: int):
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"... [trimmed {len(value) - limit} chars]"
    if isinstance(value, dict):
        return {k: _trim(v, limit, max_items) for k, v in value.items()}
    if isinstance(value, list):
        kept = [_trim(v, limit, max_items) for v in value[:max_items]]
        if len(value) > max_items:
            kept.append(f"... [trimmed {len(value) - max_items} items]")
        return kept
    return value


def trim_entry(entry: dict, limit: int, max_items: int = 50) -> dict:
    """Shorten long extra fields. The seven required keys are never changed."""
    return {k: (v if k in REQUIRED_KEYS else _trim(v, limit, max_items)) for k, v in entry.items()}


def fit_log(entries: list, max_bytes: int = MAX_LOG_BYTES) -> tuple:
    """(entries, fits). Trims harder level by level until the log fits; never drops an entry."""
    if log_size(entries) < max_bytes:
        return entries, True
    trimmed = entries
    for limit, items in zip(TRIM_LEVELS, MAX_LIST_ITEMS):
        trimmed = [trim_entry(e, limit, items) for e in entries]
        if log_size(trimmed) < max_bytes:
            return trimmed, True
    return trimmed, False


def tag_entries(entries: list, run_id: str, demo: Demo, rename: dict) -> list:
    """Add the demo tag (and the run_id when missing); point output files at their committed names."""
    tagged = []
    for entry in entries:
        e = dict(entry)
        e.setdefault("run_id", run_id)
        e["demo"] = demo.tag
        if isinstance(e.get("output_file"), str) and e["output_file"] in rename:
            e["output_file_committed"] = rename[e["output_file"]]
        tagged.append(e)
    return tagged


def append_to_root_log(root_log: Path, new_entries: list) -> tuple:
    """(entry count, fits). Appends, then trims text so the file stays under the size limit."""
    merged, fits = fit_log(read_log(root_log) + list(new_entries))
    write_log(root_log, merged)
    return len(merged), fits


def session_entry(action: str, session_id: str, **extra) -> dict:
    entry = {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "agent": "demo_runner", "action": action, "sources": [], "model": None, "cost": 0,
             "approved_by": None, "run_id": session_id}
    entry.update(extra)
    return entry


# --- reading a finished run ------------------------------------------------------------

def summarise_run(entries: list, result: DemoResult) -> DemoResult:
    """Fill the outcome, tokens, model, gates and founder actions from the run's own log."""
    models = [e.get("model") for e in entries if isinstance(e.get("model"), str) and e.get("model")]
    result.model = models[0] if models else None
    ends = [e for e in entries if e.get("action") == "end_run"]
    if ends:
        end = ends[-1]
        result.status, result.outcome = end.get("status"), end.get("outcome")
        result.tokens = int(end.get("tokens") or 0)
        result.cost = end.get("cost") or 0
    open_gate = None
    for e in entries:
        action = e.get("action")
        if e.get("agent") == "gate" and action not in GATE_META_ACTIONS and "outcome" in e:
            open_gate = {"where": f"{e.get('phase')}.{e.get('step')}", "gate": action,
                         "system": e.get("outcome"), "founder": None, "final": e.get("outcome"),
                         "basis": e.get("basis"), "phase": e.get("phase")}
            result.gates.append(open_gate)
        elif e.get("agent") == "gate" and action in FOUNDER_GATE_ACTIONS and open_gate is not None:
            if action == "founder_review_none":
                open_gate["founder"] = "no decision (recommendation stood)"
            else:
                open_gate["founder"] = ("confirmed" if action == "founder_confirm"
                                        else f"override to {str(e.get('outcome')).upper()}")
                open_gate["final"] = e.get("outcome")
            open_gate = None
        if action in ("approval_granted", "approval_denied", "founder_decision"):
            result.founder_actions.append(f"{action}: {e.get('kind')} - {e.get('decision') or e.get('outcome')}")
    return result


# --- the summary file ---------------------------------------------------------------------

def _up(value) -> str:
    return str(value).upper() if value else "n/a"


def render_summary(results, model: str, host: str, started_at: str, wall_seconds: float, fast: bool,
                   log_entries: int, log_fits: bool) -> str:
    total_tokens = sum(r.tokens for r in results)
    total_cost = sum(r.cost or 0 for r in results)
    lines = [
        "# Demo run summary",
        "",
        "> Generated by `scripts/demo_run.py` from the real runs below. Nothing in this file was typed by hand.",
        "> A team member played the founder role and answered every gate and approval prompt in the terminal.",
        "",
        "## Outcomes as they came out",
        "",
        "| Demo | Brief | Phases | Final outcome | Run status | Expected |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        d = r.demo
        lines.append(f"| {d.key} ({d.label}) | `{d.brief}` | {d.phases or '1'} | {_up(r.outcome)} | "
                     f"{r.status or 'n/a'} | {d.expected} |")
    lines += [
        "",
        "## Run facts",
        "",
        "| Item | Value |",
        "|---|---|",
        f"| Model | `{model}` (open-weight, run locally via Ollama) |",
        f"| Cost | ${total_cost:.2f} (local model) |",
        f"| Tokens | {total_tokens} in total |",
        f"| Wall time | {wall_seconds:.0f} s in total, including the founder's answers |",
        f"| Started | {started_at} |",
        f"| Research budget | {'fast (smaller)' if fast else 'full'} |",
        f"| Decision log | `decision_log.json`, {log_entries} entries"
        f"{'' if log_fits else ' (over the size limit even after trimming)'} |",
        "",
        "## What was MOCKED or STUBBED",
        "",
        "- Demo B Phase 2 sign-up evidence is MOCKED: 12 sign-ups from 400 visitors were typed in with "
        "`--mock-signups 12 --mock-visitors 400`. It is a demonstration, not a real validation.",
        "- Publishing, the waitlist form and every other outbound action are MOCKED: nothing left this machine.",
        "- Phases 3 to 6 are STUBBED in this run.",
        "- Web research and the model calls were real.",
        "",
    ]
    for r in results:
        d = r.demo
        lines += [f"## Demo {d.key}: {d.label} idea", ""]
        lines += [f"- Brief: `{d.brief}`",
                  f"- Run id: `{r.run_id or 'n/a'}`",
                  f"- Command: `python orchestrator.py {' '.join(d.cli_args(fast))}`",
                  f"- Wall time: {r.wall_seconds:.0f} s; tokens: {r.tokens}; model: `{r.model or model}`",
                  f"- Final outcome: {_up(r.outcome)} (status: {r.status or 'n/a'})"]
        if d.mocked:
            lines.append(f"- MOCKED evidence: {d.mock_signups} sign-ups / {d.mock_visitors} visitors (MOCKED)")
        lines.append("")
        if r.gates:
            lines += ["| After step | Gate | System outcome | Founder | Final |", "|---|---|---|---|---|"]
            for g in r.gates:
                basis = f" ({g['basis']})" if g.get("basis") in ("MOCKED", "STUBBED") else ""
                lines.append(f"| {g['where']} | {g['gate']} | {_up(g['system'])}{basis} | "
                             f"{g['founder'] or 'n/a'} | {_up(g['final'])} |")
            lines.append("")
        if r.founder_actions:
            lines += ["Founder decisions on guarded actions:", ""] + [f"- {a}" for a in r.founder_actions] + [""]
        if r.files:
            lines += ["Files:", ""] + [f"- `{n}`" for n in r.files] + [""]
        if r.skipped:
            lines += ["Not copied:", ""] + [f"- `{n}`: {why}" for n, why in r.skipped] + [""]
        if r.problems:
            lines += ["Problems:", ""] + [f"- {p}" for p in r.problems] + [""]
    return "\n".join(lines).rstrip("\n") + "\n"


# --- the session ----------------------------------------------------------------------------

def default_runner(root: Path, args: list) -> int:
    """Run the orchestrator CLI (``orchestrator.main``) in this terminal, with its own ``CLIApprover``.

    The founder sees and answers every prompt. Runs in this process, so stdin stays the terminal.
    """
    import orchestrator
    previous = os.getcwd()
    os.chdir(root)
    try:
        return orchestrator.main(list(args))
    except SystemExit as e:   # argparse rejects an argument
        return e.code if isinstance(e.code, int) else 2
    finally:
        os.chdir(previous)


def run_demo(root: Path, demo: Demo, runner, fast: bool, root_log: Path, echo=print) -> DemoResult:
    result = DemoResult(demo)
    runs_dir = root / "runs"
    before = {p.name for p in runs_dir.iterdir()} if runs_dir.is_dir() else set()
    echo(f"\n=== Demo {demo.key}: {demo.brief} (expected: {demo.expected}) ===")
    started = time.monotonic()
    try:
        result.returncode = runner(root, demo.cli_args(fast))
    except Exception as e:   # report it in the summary, never a stack trace in front of the founder
        result.returncode = 1
        result.problems.append(f"The orchestrator failed: {type(e).__name__}: {e}")
        echo(f"Demo {demo.key}: the orchestrator failed ({type(e).__name__}: {e}).")
    finally:
        result.wall_seconds = round(time.monotonic() - started, 1)
    run_dir, warning = find_new_run_dir(runs_dir, before)
    if warning:
        if run_dir is None:
            result.problems.append(warning)
        else:
            echo(f"Warning: {warning}")
    if result.returncode not in (0, None):
        result.problems.append(f"orchestrator.py exited with code {result.returncode}.")
    if run_dir is None:
        return result
    result.run_id = run_dir.name
    entries = read_log(run_dir / "decision_log.json")
    pairs, result.skipped = collect_run_files(run_dir, demo.label)
    result.files = copy_run_files(pairs, root / "outputs")
    rename = {src.name: new for src, new in pairs}
    summarise_run(entries, result)
    tagged = tag_entries(entries, result.run_id, demo, rename)
    tagged.append(session_entry("merge_run_log", result.run_id, demo=demo.tag, entries_merged=len(entries),
                                files=result.files, mock_evidence_phases=demo.mock_evidence_phases,
                                mock_evidence={"signups": demo.mock_signups, "visitors": demo.mock_visitors}
                                if demo.mocked else None))
    result.entries = len(entries)
    count, fits = append_to_root_log(root_log, tagged)
    if not fits:
        result.problems.append(f"decision_log.json is still over {MAX_LOG_BYTES} bytes after trimming.")
    for name, why in result.skipped:
        result.problems.append(f"{name} was not copied: {why}")
    echo(f"Demo {demo.key}: outcome {_up(result.outcome)}, {len(result.files)} files copied to outputs/, "
         f"{len(entries)} log entries appended (root log now {count} entries).")
    return result


def run_session(root: Path, demos, runner=default_runner, fast: bool = False, model: str = "",
                host: str = "", echo=print) -> tuple:
    """Reset the root log ONCE, run every demo (each appends), write the summary. Returns (results, ok)."""
    root_log = root / "decision_log.json"
    outputs = root / "outputs"
    session_id = "demo-session-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    removed = clean_previous_demo_files(outputs, [d.label for d in demos])
    if removed:
        echo(f"Removed {len(removed)} file(s) from an earlier demo run in outputs/.")
    write_log(root_log, [session_entry("start_demo_session", session_id, demos=[d.tag for d in demos],
                                       fast=bool(fast), reset_root_log=True)])
    started = time.monotonic()
    results = []
    for demo in demos:
        results.append(run_demo(root, demo, runner, fast, root_log, echo))
    wall = round(time.monotonic() - started, 1)
    count, fits = append_to_root_log(root_log, [session_entry(
        "end_demo_session", session_id, wall_seconds=wall,
        outcomes={r.demo.tag: r.outcome for r in results}, tokens=sum(r.tokens for r in results),
        summary_file=SUMMARY_NAME)])
    text = render_summary(results, model, host, started_at, wall, fast, count, fits)
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / SUMMARY_NAME).write_text(text, encoding="utf-8", newline="\n")
    ok = fits and all(not r.problems for r in results)
    return results, ok


def _confirm_start(read=input) -> bool:
    print("\nThis resets decision_log.json at the repository root and replaces earlier demo files in outputs/.")
    print("You will then act as the founder: read each gate and approval prompt and answer it yourself.")
    try:
        answer = read("Start the demo run? [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in ("y", "yes")


def main(argv=None, root: Path = ROOT) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="The committed demo run (a human answers every prompt).")
    parser.add_argument("--fast", action="store_true", help="smaller Phase 1 research budget in both demos")
    args = parser.parse_args(argv)
    root = Path(root)

    try:
        tty = bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, ValueError):
        tty = False
    host, model = ollama_settings()
    print("Checking that everything is ready (nothing is written yet)...")
    problems = preflight(root, DEMOS, tty, host, model)
    if problems:
        print("\nThe demo cannot start yet:\n", file=sys.stderr)
        for p in problems:
            print(f"- {p}\n", file=sys.stderr)
        return 2
    print(f"Ready: Ollama at {host}, model '{model}'.")
    if not _confirm_start():
        print("Not started. Nothing was changed.")
        return 1
    try:
        results, ok = run_session(root, DEMOS, fast=args.fast, model=model, host=host)
    except KeyboardInterrupt:
        print("\nStopped by the user. decision_log.json and outputs/ hold a partial run: "
              "run this script again before committing.", file=sys.stderr)
        return 130
    print("\nDEMO RUN FINISHED")
    for r in results:
        print(f"- Demo {r.demo.key} ({r.demo.label}): {_up(r.outcome)} in {r.wall_seconds:.0f} s, "
              f"{r.tokens} tokens, run {r.run_id}")
        for p in r.problems:
            print(f"    problem: {p}")
    print(f"Summary: outputs/{SUMMARY_NAME}")
    print("Next: python scripts/validate_demo.py   (then scripts/payment_test.py --demo, which appends to the log)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
