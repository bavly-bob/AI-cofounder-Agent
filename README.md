# AI Cofounder

An open-source team of AI agents that takes a founder's idea toward a validated digital product, with founder approval at every gate. This is the M0 skeleton: only the local model connection works so far.

## Quickstart

```bash
docker compose up
```

Open http://localhost:8501. The first run downloads the model (~5 GB), so it can take a while.

## What works and what is mocked

| Step | Status |
|---|---|
| Local model connection | Working |
| Phase 1: Find opportunities | STUBBED (planned) |
| Phase 2: Design the business | STUBBED (planned) |
| Phase 3: Build the product | STUBBED (planned) |
| Phase 4: Set the price and policies | STUBBED (planned) |
| Phase 5: Open the store; deliver automatically (payments and delivery) | MOCKED |
| Phase 6: Operate and learn | STUBBED (planned) |
| Gates and founder approval | STUBBED (planned) |

## Architecture

TODO: the app is a Streamlit page that calls a local Ollama model through `llm.py`; the full six-phase design will be described in [ARCHITECTURE.md](ARCHITECTURE.md).

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
