# 3-minute live demo plan

Key facts: the demo uses the app (`docker compose up`, then http://localhost:8501) with the local model already pulled, and a team member acting as the founder clicks every approval. Nothing in the demo publishes, sends or charges anything: those parts are MOCKED, and the app says so in its banner. Start the app and warm the model up before the session; a full Phase 1 run on a laptop CPU can take several minutes, so the demo shows one run started live and a finished run of the same brief, opened from "Saved runs".

## Before the session

- `docker compose up`; wait until the sidebar shows the model as ready.
- Run the decent brief once to the end (Phase 2 with mock numbers 12 / 400) and the weak brief once (Phase 1), so both appear under "Saved runs".
- Set "Fast mode" on. Have the decision log tab and the Outputs tab ready.

## Timeline

| Time | What to show | What to say |
|---|---|---|
| 0:00-0:20 | The app, the MOCKED / STUBBED banner, the sidebar model status (local, $0.00) | An open-source agent team that takes an idea toward a validated digital product; the founder decides every gate; local open-weight model, no paid service. |
| 0:20-0:50 | Load the weak example brief, press Start run | The agents search the free web, cite only pages they retrieved, and label everything else ASSUMPTION. |
| 0:50-1:20 | Open the saved weak run: the timeline and the Phase 1 gate file in the Outputs tab | The gate file shows its target (set before the step), the actual numbers, and the STOP recommendation on weak evidence, with the stop memo saying what evidence would change it. |
| 1:20-2:00 | Open the saved decent run: the Lean Canvas, the experiment target approval with its SHA-256, the landing page with the "MOCKED — demo form" label | The target was approved before any result and is locked by its hash; the page has no fake proof, no timers, no promises. Publishing writes a local copy only. |
| 2:00-2:30 | The Phase 2 gate file on the MOCKED 12 / 400 numbers; then the live run's gate card if it has reached one | 12 of 100 is below half the target, so the system recommends STOP (or PIVOT if a Phase 1 alternative scored well), and the file says the numbers are MOCKED, not a real validation. On a gate card: Approve, Reject, or Override, which needs a written reason. |
| 2:30-2:50 | The Decision log tab | One entry per action, with sources, model, cost 0 and `approved_by: founder` on every approved action. |
| 2:50-3:00 | Ask the agents: type a request for fake reviews | Refused by the guardrails, with an honest alternative. Close: Phases 3-6 are designed and on the roadmap. |

## Fallback

If the model is slow or offline, use only the saved runs and the committed demo files in `outputs/`; never type numbers or results by hand.
