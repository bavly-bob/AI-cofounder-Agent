# Contributing

Thank you for helping. AI Cofounder is MIT-licensed, runs on a local open-weight model, and must never need a paid service.

## Getting started

```bash
docker compose up            # app at http://localhost:8501
```

or, for development without Docker (Python 3.11+, Ollama running locally):

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip install -r requirements.txt
ollama pull qwen3:8b
pytest -q                                     # offline: no model and no network needed
```

## Where things are

- `orchestrator.py`, `steps.py`: the run loop and the step contract. A new agent is a class in `agents/<role>.py` with `phase`, `step`, `name`, `agent` and `run(ctx)`, decorated with `@register_step`, plus a prompt file `prompts/<role>.md`.
- `gates.py`, `approval.py`: gates and founder approval. `guardrails.py`: safety rules. `decision_log.py`: the log.
- `app.py`, `ui/`: the Streamlit app. `tools/`: free web search and fetch.
- `ARCHITECTURE.md` and `docs/`: how it all fits together.

## Rules for every change

- Run `pytest -q` and keep it green. Add a smoke test for new behaviour in `tests/` (`test_*.py`).
- Nothing may publish, send, spend or sign without `approval.guarded_action` and an explicit founder approval. No auto-approve flag, setting or environment variable in app code.
- No paid APIs, no API keys, no payment SDKs. Payments stay MOCKED until a founder-owned test-mode integration is designed.
- Label anything simulated `MOCKED` and anything not built `STUBBED`, in the code, the UI and the README table.
- Never hand-write files in `outputs/` or entries in `decision_log.json`; they come only from real runs.
- Every market claim cites a retrieved source URL or is labelled `ASSUMPTION`.
- Never commit secrets: real settings go in an untracked `.env`; `.env.example` holds placeholders only.
- Pin new dependencies in `requirements.txt` and add them with their licence to `docs/dependencies.md` (OSI-approved licences only).
- Guardrail test prompts ("trick prompts") belong under `tests/` or `evals/` only.

## Good first issues

Look for issues labelled `good first issue`: new benign and attack prompts for `evals/`, more languages in the guardrails, UI wording, and documentation fixes.

## Pull requests

Keep a pull request to one topic, describe what changed and how you tested it, and update `ARCHITECTURE.md` and the README table when a status changes. By contributing you agree that your work is released under the MIT License and that you follow the [Code of Conduct](CODE_OF_CONDUCT.md).
