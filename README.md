# AI Cofounder

An open-source team of AI agents that takes a founder's idea toward a validated digital product, with a founder confirm-or-override step at every gate. Phase 1 (Discover) and Phase 2 (Validate: Lean Canvas, a pre-registered demand test, an honest landing page, an evidence gate) work with a local model; publishing and the waitlist form are MOCKED, sign-up numbers are founder-supplied (MOCKED in the demo), and Phases 3-6 are not built yet.

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
| Step 1.1 Find opportunities: problem, sourced pain signals, 2-3 alternatives (free web search, no API key) | Working |
| Step 1.2 Score ideas: model sub-scores, weighted total computed in code | Working |
| Step 1.3 Market: segment, beachhead, sourced competitors, market size only if sourced | Working |
| Step 1.4 Interviews: Mom Test script, recruiting plan, outreach drafts, summary of real notes | Working |
| Phase 1 evidence gate (go / repeat / pivot / stop on sourced evidence only) | Working |
| Outreach sending (founder approval required) | MOCKED |
| Step 2.1 Lean Canvas: 9 boxes, sourced or ASSUMPTION, price hypothesis, 3 riskiest assumptions (founder sign-off) | Working |
| Step 2.2 Experiment design: target approved by the founder before results, locked with a SHA-256 hash | Working |
| Step 2.3 Landing page generation: one self-contained HTML file, checked for fake proof, urgency and promises | Working |
| Landing page publishing (founder approval required; writes a local copy only) | MOCKED |
| Waitlist form on the landing page (submits nowhere, labelled on the page) | MOCKED |
| Step 2.4 Demand results: sign-up data from the founder (`--waitlist` CSV or `--signups`), or typed in for the demo (`--mock-signups`) | Working; demo data MOCKED |
| Phase 2 demand gate (go / repeat / pivot / stop against the pre-registered target) | Working |
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
