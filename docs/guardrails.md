# Guardrails

Key facts: the rules layer (`guardrails.py`, Layer 1) is always on, needs no model and no network, and covers 10 categories in English and Arabic (including Egyptian Arabic wording). An optional Layer 2 local-model classifier is built and tested (`check_request_layered`), but it is STUBBED in the status table: only the evals call it, and the pipeline and the app use Layer 1. Every refusal is logged by the caller, and a check that crashes fails closed (blocked).

## Categories

| Category | What is refused |
|---|---|
| `fake_social_proof` | fake reviews, testimonials, ratings, sign-up or customer numbers |
| `income_promise` | promises or guarantees of income, results or success |
| `fake_urgency` | countdown timers, invented deadlines, "only X left" |
| `impersonation` | speaking as the founder, a customer or another brand |
| `credential_storage` | storing passwords, card numbers or similar secrets |
| `money_handling` | holding, moving or refunding money with no founder decision |
| `paywall_core` | hiding core features behind our own paywall |
| `excluded_product` | physical goods; financial, investment or crypto products; health or legal advice |
| `approval_bypass` | acting without the founder's explicit approval, or setting the rules aside |
| `gate_tampering` | changing a gate target or outcome after the results are in |

Each refusal comes with a polite reason and an honest alternative (for an excluded product, a compliant reframing).

## Where each check runs

| Check | Called from | Effect when it refuses |
|---|---|---|
| `check_brief` | `orchestrator.py`, before any step; the Phase 1 gate (excluded product) | STOP memo; the founder cannot override it at a gate |
| `check_request` | `orchestrator.py` before every step; "Ask the agents" in `ui/ask.py` | STOP memo, or a refusal shown in the app |
| `check_output` | `orchestrator.py` on every step file before saving; `agents/common.py` on every model field; `ui/ask.py` on every answer | step file: not saved, the step is retried, then STOP; a model field: dropped and logged; an answer: refused |
| `check_landing_page` | `agents/copywriter.py` before the page is saved | the page is rebuilt with neutral fallback copy; if that also fails, the step fails |
| `check_request_layered` | `evals/run_evals.py` only (not yet the pipeline or the app) | Layer 1 first; the classifier runs only if Layer 1 allows |

On the output side, `check_output` and `check_landing_page` also block invented user or customer counts, quotes attributed to people, and income or revenue projections (test cases in `evals/output_cases.json`). `check_landing_page` blocks testimonials, reviews or stars, "join N others" and other counts or percentages, countdowns and "only X left" (in text or script code), income promises, other brands shown as partners, the founder's first-person voice, and outside scripts, trackers, frames or form targets. Any form must show the visible label "MOCKED — demo form".

## Layer 2 (optional classifier)

`check_request_layered(text, llm_client=None, side_effect=None)` runs Layer 1, then, only if Layer 1 allows, asks a local model with the prompt in `prompts/guardrail.md`. The request is wrapped as data and markers inside it are neutralised. With no client it says "Layer 1 only". If the classifier fails, a request that would publish, send, spend or sign is held for the founder, and a plain content request keeps the Layer 1 result. No environment variable switches it on or off. Tests use a stand-in classifier (`tests/test_guardrails_layer2.py`); the real-model mode of the evals has not been run yet.

## Evals

`python evals/run_evals.py --rules-only` runs the attack prompts in `evals/trick_prompts.json` (148), the benign requests in `evals/benign_prompts.json` (51) and the output-side cases in `evals/output_cases.json`, and writes [evals/guardrail_report.md](../evals/guardrail_report.md). The current report (rules only, Layer 1) shows 148/148 attacks blocked (100.0%), 147/148 under the expected category, and 0/51 benign requests refused (0.0%). Output side: `check_output` blocked 14/14 must-block cases and refused 0/10 must-allow cases; `check_landing_page` blocked 14/14 and refused 0/10. These are **in-sample** numbers: the rules were written with these prompts in view, so they do not measure performance on new wording.

The trick prompts live only under `evals/` and `tests/`.
