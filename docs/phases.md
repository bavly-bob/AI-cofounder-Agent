# Phases in detail

Key facts: Phases 1 and 2 are built and run on a local model; Phase 5 sale to cash is a MOCKED simulation; Phases 3, 4, 6 and opening the store are STUBBED (designed, not built). The summary per phase is in [ARCHITECTURE.md](../ARCHITECTURE.md#phases); this file adds the step-by-step detail.

## How a step runs

For each step, `orchestrator.py`:

1. logs the targets of the step's gates (and, at the start of a phase, the thresholds of every gate in it);
2. checks the step's request with `guardrails.check_request` (a refusal is STOP);
3. runs the step (`run(ctx)`); a model that cannot be reached is a clean STOP with a memo;
4. renders the result with a header line (`run_id`, model, status WORKING / MOCKED / STUBBED) and every claim with its source link or "ASSUMPTION";
5. checks the rendered file with `guardrails.check_output` (refused: not saved, retried, then STOP);
6. saves the file (and any extra HTML file), logs the action, runs the gates and asks the founder.

An empty or near-empty idea (fewer than 3 letters) stops before any model or web call with a request to describe the idea.

## Phase 1: Discover

| Step | Agent | Prompt | What it produces |
|---|---|---|---|
| 1.1 opportunities | `agents/researcher.py` | `prompts/researcher.md` | problem statement, target user, sourced pain signals, 2-3 alternatives, the search queries used |
| 1.2 idea_scoring | `agents/scorer.py` | `prompts/scorer.md` | 1-5 sub-scores with a reason each; pain evidence from the count of sourced signals; weighted total in Python; any hard founder-fit blocker |
| 1.3 market | `agents/market_analyst.py` | `prompts/market_analyst.md` | segment, beachhead and use case (ASSUMPTION), competitors with retrieved sources, market size only if sourced |
| 1.4 interviews | `agents/interviewer.py` | `prompts/interviewer.md` | 8-10 Mom Test questions, who to recruit and where, an outreach DRAFT, a summary of real notes if `--interviews` was given |
| gate | `gates.py` | none | `Phase1EvidenceGate` |
| 1.5 outreach | `agents/interviewer.py` | none | asks the founder whether to send the draft; MOCKED send |

Research limits: 8 searches and 10 page fetches per run (5 and 5 with `--fast`); environment settings can lower them, never raise them. Search results and fetched pages are cached in `runs/<run_id>/sources/`. A REPEAT of step 1.1 must use different queries.

Not built: simulated interview personas (planned, then cut for time).

## Phase 2: Validate

Phase 2 starts only after a Phase 1 GO (or a founder override to GO). It can run in the same run (`--phases 1,2`) or later on a saved run (`--resume <run_id> --from phase2`), which reuses the Phase 1 results and any Phase 2 step that already passed its gate, such as an approved target.

| Step | Agent | Prompt | What it produces | Gate |
|---|---|---|---|---|
| 2.1 lean_canvas | `agents/strategist.py` | `prompts/strategist.md` | 9 boxes (sourced from Phase 1 or ASSUMPTION), price hypothesis, 3 riskiest assumptions with cheap tests | founder sign-off; No redoes the step with the note |
| 2.2 experiment | `agents/experimenter.py` | `prompts/experimenter.md` | hypothesis, channels, kill criteria, core message, target and its SHA-256 | founder approves the target before results; No designs a new experiment |
| 2.3 landing_page | `agents/copywriter.py` | `prompts/copywriter.md` | a Markdown summary and one HTML page; MOCKED form | founder approves the MOCKED publish (local copy only) |
| 2.4 demand_results | `agents/analyst.py` | none (no model) | evidence type, sign-ups, visitors, conversion, days elapsed, plain interpretation | `Phase2DemandGate` |

Evidence for step 2.4, in priority order: a waitlist CSV (`--waitlist`: invalid emails and duplicates dropped, rows dated outside the test window dropped, only counts kept), founder-reported numbers (`--signups`, `--visitors`), MOCKED demo numbers (`--mock-signups`, `--mock-visitors`). An unreadable `--waitlist` export is STOP with that reason; no other numbers are used instead. In the app, the run options (fast mode, phases, founder-reported and mock numbers) reach the engine; an uploaded CSV is only counted in memory (non-empty rows minus the header, with no de-duplication and no date window, and no email stored), and that count is passed as founder-reported sign-ups unless sign-ups were typed in. For de-duplication and the test window, use the CLI `--waitlist` flag.

In the app, `ui/engine_adapter.py` calls `orchestrator.start_run`, `step_until_pause` and `submit_decision`. The run pauses at every founder question, and its state is saved in `runs/<run_id>/state.json` after every step, gate, pause and decision, so a restarted app shows the same pending item and continues after the founder answers.

## Phases 3-6 (designed, not built)

- **Phase 3 Build:** builder agent (spec, then code), test agent (writes and runs tests), docs agent (user docs). Gate: tests pass (`TestsPassGate`) and founder approval.
- **Phase 4 Commercial setup:** pricing agent (options from sourced competitor prices and the founder's values), policy agent (terms, privacy, refund drafts labelled "draft, not legal advice — review by a qualified person"). Gate: founder approval. No payments.
- **Phase 5 Launch:** offer page published only via `guarded_action("publish")` (STUBBED); sale to cash is the MOCKED simulation in [sale_to_cash.md](sale_to_cash.md).
- **Phase 6 Operate and learn:** support drafts sent only after founder approval; a metrics agent working from the decision log and MOCKED events; a persevere-or-pivot memo. Gate: evidence threshold.
