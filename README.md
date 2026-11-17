# AI Cofounder

An open-source team of AI agents that takes a founder's idea toward a validated digital product, with a founder confirm-or-override step at every gate. The core engine works (orchestrator, gates, founder approval, decision log, guardrails); the agents behind each step are still STUBBED.

## Quickstart

```bash
docker compose up
```

Open http://localhost:8501. The first run downloads the model (~5 GB), so it can take a while.

## What works and what is mocked

| Step | Status |
|---|---|
| Local model connection (Ollama) | Working |
| Founder brief: load from JSON, or parse free text with a safe fallback | Working |
| Orchestrator: runs steps in order, saves each output, CLI summary | Working |
| Gates: go / repeat / pivot / stop, with a stop memo on weak or missing evidence | Working |
| Founder approval before any publish, send, spend or sign (enforced in code, default No) | Working |
| Decision log: one validated entry per agent action | Working |
| Guardrails: brief, request and output checks | Working |
| Step 1.1 Find opportunities (dummy step) | STUBBED |
| Step 2.1 Validate demand (dummy step; its metric is typed in, not measured) | STUBBED, metric MOCKED |
| Phase 3: Build the product | STUBBED (planned) |
| Phase 4: Set the price and policies | STUBBED (planned) |
| Publish (offer page) and send (messages) | MOCKED |
| Phase 5: Payments and delivery | MOCKED |
| Phase 6: Operate and learn | STUBBED (planned) |

Run the engine from the command line (results go to `runs/<run_id>/`):

```bash
python orchestrator.py --brief examples/briefs/weak_idea.json --metric 12
```

## Architecture

`orchestrator.py` runs each step (`steps.py`) and then its gates (`gates.py`), which recommend go, repeat, pivot or stop against a target fixed before the step runs. Any publish, send, spend or sign action goes through `approval.py` and runs only after the founder says yes. `guardrails.py` checks the brief and every output, and `decision_log.py` records every action. The model is a local Ollama model called through `llm.py`. The full six-phase design will be described in [ARCHITECTURE.md](ARCHITECTURE.md).

## Models and free resources

The app uses the open-weight model `qwen3:8b`, run locally with Ollama, so no paid API or key is required. To use a smaller model on a weaker laptop, set `OLLAMA_MODEL` (for example `qwen3:4b` or `qwen3:1.7b`) in a `.env` file or the shell before `docker compose up`.

## Cost per run

About $0.00 per run (local model)

## Demo video

TODO: no demo video has been recorded yet.

## What our system will NOT do

TODO: this will list our limits; it will never promise income, fake reviews or sign-ups, pose as anyone, or move money by itself.

## How the project stays alive after the hackathon

TODO: maintainers, roadmap, community and funding are not decided yet.
