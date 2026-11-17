# 3-minute live demo plan

Key facts: the demo uses the app (`docker compose up`, then http://localhost:8501) with a local model already pulled, and a team member acting as the founder clicks every approval. Nothing in the demo publishes, sends or charges anything: those parts are MOCKED, and the app says so in its banner. Timings below come from the committed demo run ([outputs/phase0_step0_demo_summary.md](../outputs/phase0_step0_demo_summary.md)): with `qwen3:1.7b` and the fast research budget, Phase 1 took 80 s for the weak brief and 43 s for the decent brief, including the founder's answers. Both briefs stopped at the Phase 1 gate, so the demo shows no Phase 2 run; Phase 2 is covered by the tests in `tests/test_validate_m3.py`.

## Before the session

- Set `OLLAMA_MODEL=qwen3:1.7b` (what the committed demo used) unless the machine has the memory for `qwen3:8b`; run `docker compose up` and wait until the sidebar shows the model as ready.
- Warm the model up with one short run, then press New run.
- Set "Fast mode" on and "Run up to phase" 1. Open the committed files in `outputs/` in a second tab: the two gate files, the stop memos and [payment_test_log.md](../outputs/payment_test_log.md).

## Timeline

| Time | What to show | What to say |
|---|---|---|
| 0:00-0:20 | The app, the MOCKED / STUBBED banner, the sidebar model status (local, $0.00) | An open-source agent team that takes an idea toward a validated digital product; the founder decides every gate; local open-weight model, no paid service. |
| 0:20-0:30 | Load the weak example brief, press Start run | The agents search the free web, cite only pages they retrieved, and label everything else ASSUMPTION. The weak brief took 80 s in the committed run. |
| 0:30-1:10 | While it runs: the committed decent-brief gate, [phase1_gate_discover_decent.md](../outputs/phase1_gate_discover_decent.md) | The target was set before the step ran. Two sourced signals were found and three are required, so the system says STOP, even for the brief we expected to pass. It lists what evidence would change the decision. |
| 1:10-1:50 | Back in the app: the live run's gate card (weak brief) | Target, actual and reasons. Show Approve, Reject and Override (Override needs a written reason). Approve the STOP; the stop memo appears in Outputs. |
| 1:50-2:20 | [payment_test_log.md](../outputs/payment_test_log.md) | A MOCKED sale-to-cash run: a duplicate webhook delivered once, a refund only after the founder's approval with access revoked, and a planted payout mismatch found by reconciliation. No real money. |
| 2:20-2:45 | The Decision log tab | One entry per action, with sources, model, cost 0 and `approved_by: founder` on every founder decision. |
| 2:45-3:00 | Ask the agents: type a request for fake reviews | Refused by the guardrails, with an honest alternative. Close: Phase 2 is built and tested; Phases 3, 4 and 6 are designed and on the roadmap. |

## Fallback

If the live run is slow (the weak brief took 80 s) or the model is offline, use only the committed files in `outputs/`. Never type numbers or results by hand.
