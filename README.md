# AI Cofounder

An open-source team of AI agents that takes a founder's idea toward a validated digital product, with the founder confirming or overriding every gate and approving every outside action. Phase 1 (Discover) and Phase 2 (Validate) run end to end on a local open-weight model with free, keyless web search. Publishing, sending, the waitlist form, demo sign-up numbers and the sale-to-cash flow are MOCKED; Phases 3, 4 and 6 and the store are STUBBED (not built yet).

## Quickstart

```bash
docker compose up
```

Open http://localhost:8501.

The first start downloads the model (`qwen3:8b`, about 5 GB), which can take a while; the app shows "Model loading" until it is ready.

1. Load a brief: pick an example (`examples/briefs/decent_idea.json` or `examples/briefs/weak_idea.json`), paste free text, or fill in the form.
2. Choose "Run up to phase" 1 or 2 in the sidebar (optionally founder-reported or MOCKED sign-up numbers for Phase 2), then press **Start run**.
3. At each gate card, **Approve** the recommendation, **Reject** (halts the run) or **Override** with a written reason. Nothing is published, sent, spent or signed without your click. Results are in the Outputs and Decision log tabs.

Command line (results go to `runs/<run_id>/`). For the CLI: `pip install -r requirements.txt`, then either `docker compose up -d ollama ollama-init` (Ollama on port 11434, model pulled) or a local Ollama with `ollama pull qwen3:8b`.

```bash
python orchestrator.py --brief examples/briefs/decent_idea.json --phases 1,2 --fast --mock-signups 12 --mock-visitors 400
```

## What works and what is mocked

| Component | File | Status | Verifying test |
|---|---|---|---|
| Founder brief: JSON file, free text parsed by the local model with a safe fallback, or the form in the app | `brief.py` | Working | `test_engine_m1.py::test_example_briefs_load`, `test_parse_brief_text_never_crashes` |
| Orchestrator: steps in order, a gate after each step, outputs saved, resume from Phase 2 | `orchestrator.py`, `steps.py` | Working | `test_engine_m1.py::test_cli_prints_a_summary_and_writes_only_under_runs`, `test_validate_m3.py::test_resume_from_phase2_reuses_phase1_and_the_approved_target` |
| Gates: go / repeat / pivot / stop, target logged before the step, missing evidence is STOP, stop memo | `gates.py` | Working | `test_engine_m1.py::test_missing_or_unsourced_evidence_never_passes`, `test_gate_target_is_logged_before_the_step_runs`, `test_metric_12_against_100_stops_and_writes_stop_memo` |
| Founder confirm or override at every gate, logged with both outcomes; the system never upgrades a STOP | `gates.py`, `approval.py` | Working | `test_engine_m1.py::test_system_never_upgrades_a_stop_but_the_founder_can_override`, `test_state_m4.py::test_override_requires_a_note_and_logs_both_outcomes` |
| Founder approval before any publish, send, spend or sign (enforced in code, default No, no bypass) | `approval.py` | Working | `test_engine_m1.py::test_cli_approver_default_is_no`, `test_no_side_effects_without_approval.py::test_without_approval_no_executor_runs`, `test_approval_no_bypass.py` |
| Decision log: one validated entry per action; emails, phone numbers, keys and card numbers scrubbed | `decision_log.py` | Working | `test_engine_m1.py::test_every_log_entry_has_the_required_keys`, `test_privacy.py::test_logger_scrubs_extras_but_keeps_required_keys` |
| Guardrails layer 1: rules for 10 categories in English and Arabic, on the brief, requests, outputs and the landing page (outputs: invented user counts, attributed quotes, income or revenue projections) | `guardrails.py` | Working | `test_guardrails_m1.py::test_every_trick_prompt_is_blocked_with_its_category`, `test_guardrails_landing.py::test_dishonest_content_is_blocked`, `test_output_side_dishonest_copy_is_blocked` |
| Guardrails layer 2: optional local-model request classifier (built and tested with a stand-in classifier; not yet called by the pipeline or the app) | `guardrails.py`, `prompts/guardrail.md` | STUBBED | `test_guardrails_layer2.py::test_classifier_runs_only_after_layer1_allows_and_can_block` |
| Local model client (Ollama), schema-checked JSON, one retry, clean STOP when the model is down | `llm.py` | Working | `test_discover_m2.py::test_generate_json_retries_once_then_raises`, `test_no_ollama_stops_cleanly` |
| Web app: brief form, run options incl. Phase 2 evidence, timeline, gate cards (Approve / Reject / Override with a note), decision log, Ask the agents | `app.py`, `ui/` | Working | `test_ui_m4.py::test_app_loads_with_mocked_banner_and_no_error`, `test_approve_on_a_pending_item_advances_the_run`, `test_state_m4.py::test_ui_options_reach_run_pipeline` |
| Browser run state saved after every step, gate, pause and decision; after an app restart the same pending item is shown and the run continues after the founder's decision | `run_state.py`, `orchestrator.py` | Working | `test_state_m4.py::test_restart_from_state_json_shows_the_same_pending_item_and_continues`, `test_adapter_pause_approve_continue_then_stop` |
| Step 1.1 Find opportunities: problem, sourced pain signals, alternatives (free web search, no API key) | `agents/researcher.py`, `tools/` | Working | `test_discover_m2.py::test_cited_url_not_in_registry_becomes_assumption`, `test_signal_whose_wording_does_not_match_its_source_is_downgraded` |
| Step 1.2 Score ideas: model sub-scores, weighted total computed in code | `agents/scorer.py` | Working | `test_discover_m2.py::test_weighted_score_equals_the_python_computation` |
| Step 1.3 Market: segment, beachhead, sourced competitors, market size only if sourced | `agents/market_analyst.py` | Working | `test_discover_m2.py::test_competitor_without_retrieved_source_is_dropped_and_size_is_assumption` |
| Step 1.4 Interviews: Mom Test script, recruiting plan, outreach draft, summary of the founder's real notes | `agents/interviewer.py` | Working | `test_discover_m2.py::test_mom_test_script_drops_hypothetical_questions`, `test_real_interview_notes_are_summarised_but_not_counted_by_the_gate` |
| Phase 1 evidence gate: go / repeat / pivot / stop on sourced evidence only | `gates.py` | Working | `test_discover_m2.py::test_weak_brief_with_one_signal_stops_and_writes_a_stop_memo`, `test_alternative_scoring_higher_pivots`, `test_borderline_evidence_repeats_step_1_1_once_with_new_queries` |
| Step 1.5 Outreach sending (founder approval required; nothing is sent) | `agents/interviewer.py` | MOCKED | `test_discover_m2.py::test_denied_outreach_sends_nothing_and_the_run_goes_on`, `test_mock_send_cannot_run_outside_guarded_action` |
| Step 2.1 Lean Canvas: 9 boxes, sourced or ASSUMPTION, price hypothesis, 3 riskiest assumptions, founder sign-off | `agents/strategist.py` | Working | `test_validate_m3.py::test_canvas_has_nine_boxes_and_unsourced_items_are_assumptions`, `test_canvas_rejection_repeats_the_step_with_the_founders_note` |
| Step 2.2 Experiment design: target approved by the founder before any result, locked with a SHA-256 hash | `agents/experimenter.py` | Working | `test_validate_m3.py::test_target_is_approved_before_results_and_logged_with_its_hash`, `test_target_change_after_approval_is_refused_and_the_hash_is_unchanged` |
| Step 2.3 Landing page generation: one self-contained HTML file, checked for fake proof, urgency and promises | `agents/copywriter.py` | Working | `test_validate_m3.py::test_generated_page_passes_the_check_and_is_honest`, `test_landing_page_check_blocks_dishonest_content` |
| Landing page publishing (founder approval required; writes a local copy only) | `agents/copywriter.py` | MOCKED | `test_validate_m3.py::test_publish_denied_writes_nothing`, `test_publish_approved_writes_only_the_local_mock_copy` |
| Waitlist form on the landing page (submits nowhere, labelled on the page) | `agents/copywriter.py` | MOCKED | `test_validate_m3.py::test_generated_page_passes_the_check_and_is_honest` |
| Step 2.4 Demand results from the founder's data (waitlist CSV, counts only, or founder-reported numbers); an unreadable export is STOP | `agents/analyst.py` | Working | `test_validate_m3.py::test_waitlist_csv_counts_only_and_no_email_is_written_anywhere`, `test_unreadable_waitlist_is_reported_and_other_evidence_is_not_used` |
| Demo sign-up numbers typed in with the mock flags (not real evidence) | `agents/analyst.py` | MOCKED | `test_validate_m3.py::test_mocked_go_says_not_a_real_validation_in_every_artifact`, `test_12_of_100_mocked_is_stop_and_mocked_everywhere` |
| Phase 2 demand gate against the pre-registered target (cross-checked with the logged target; one REPEAT, also across resumes) | `gates.py` | Working | `test_validate_m3.py::test_no_data_is_stop_with_the_rerun_hint`, `test_70_of_100_repeats_once_then_pivots`, `test_120_of_100_at_good_conversion_is_go`, `test_resume_fixes_m4.py::test_resume_after_a_used_repeat_does_not_grant_another`, `test_stored_target_that_differs_from_the_logged_one_is_stop` |
| Phase 3: Build the product (builder, test and docs agents) | none yet | STUBBED | none (not built) |
| Phase 4: Set the price and policies | none yet | STUBBED | none (not built) |
| Phase 5: Open the store (publish the offer page) | none yet | STUBBED | none (not built) |
| Phase 5: Payment events, delivery, refunds and reconciliation (local simulation, no real money) | `payments.py`, `webhooks.py`, `delivery.py`, `refunds.py`, `reconcile.py` | MOCKED | `test_sale_to_cash.py::test_duplicate_event_delivers_once`, `test_refund_without_approval_is_not_executed`, `test_reconciliation_finds_planted_mismatch` |
| Phase 6: Operate and learn | none yet | STUBBED | none (not built) |

All tests are in `tests/` and run offline with `pytest -q` (no Ollama, no network: the model and the web are replaced by test doubles that are passed in, never switched on by a setting).

## Architecture

`orchestrator.py` runs the registered agent steps (`agents/`) in order; before each step every gate logs its target, and after it the gates in `gates.py` recommend go, repeat, pivot or stop, which the founder confirms or overrides. Any publish, send, spend or sign action goes through `approval.guarded_action` and runs only after an explicit founder Yes; all such executors are MOCKED. `guardrails.py` checks the brief, every request and every output, and `decision_log.py` records one entry per action. Agents call a local Ollama model through `llm.py` and research the web only through the GET-only tools in `tools/`, whose text is treated as untrusted data. The Streamlit app (`app.py`, `ui/`) drives the same engine through `ui/engine_adapter.py`, which pauses the run at every founder question and saves its state after every step, gate, pause and decision. The full six-phase design, gates and sale-to-cash flow are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Models and free resources

- **Model:** the open-weight `qwen3:8b`, run locally with Ollama (the only model runtime; started by `docker compose up`). No API key and no paid service is needed. On a weaker laptop set `OLLAMA_MODEL` (for example `qwen3:4b` or `qwen3:1.7b`) in `.env` or the shell before `docker compose up`. The committed demo run used `qwen3:1.7b`, set through `OLLAMA_MODEL`, because the laptop had too little free memory for `qwen3:8b` ([demo summary](outputs/phase0_step0_demo_summary.md)).
- **Web research (free, no keys):** web search through the `ddgs` package (bing, yahoo and duckduckgo backends), the Hacker News Algolia API and the Wikipedia API. Reddit's public search is also tried, but it returned HTTP 403 from our network, so it may add nothing. Budget: at most 8 searches and 10 page fetches per run (`config/research.json`).
- Settings are listed in `.env.example` (placeholders only). Licences: [docs/dependencies.md](docs/dependencies.md).

## Cost per run

About $0.00 per run (local `qwen3:1.7b`; measured in the committed demo: about 6,000 tokens and 43-121 s per brief for Phase 1 with `--fast`, 6,011 and 5,881 tokens, wall time including the founder's answers). Figures for `qwen3:8b` or a full research budget have not been measured. Source: [outputs/phase0_step0_demo_summary.md](outputs/phase0_step0_demo_summary.md).

## Demo video

Link: TODO — add before the freeze

## What our system will NOT do

These limits are enforced in code (`guardrails.py`, `approval.py`) and tested; red-team results are in [evals/guardrail_report.md](evals/guardrail_report.md) (in-sample prompts).

- Promise income or success to buyers.
- Create fake reviews, testimonials, sign-up numbers, social proof, countdown timers or "only X left" urgency.
- Pose as the founder, a customer or another brand.
- Hold, move or refund money by itself: a refund is a founder-approved "spend" action, and every payment is a local MOCKED simulation.
- Create accounts, or store passwords or card numbers.
- Hide core features behind our own paywall.
- Publish, send, spend or sign anything without an explicit founder approval (silence or a closed input counts as No).
- Change a gate target or outcome after the results are in.
- Help with excluded products: physical goods; financial, investment or crypto products; health or legal advice.

The rules layer is pattern-based and misses some paraphrases: out-of-sample QA found misses such as "earn their money back in a week, guaranteed". Outside actions (publish, send, spend, sign) stay blocked by the founder-approval check in code regardless of what the guardrails catch.

More detail: [docs/guardrails.md](docs/guardrails.md).

## How the project stays alive after the hackathon

- **Maintainers:** TODO (name, GitHub handle) and TODO (name, GitHub handle).
- **Roadmap:** see [ARCHITECTURE.md](ARCHITECTURE.md#roadmap): the Phase 3 builder, Phase 4 pricing and policies, real payments in test mode on founder-owned accounts, Phase 6 metrics, more languages and models.
- **Community:** [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md); issues labelled `good first issue`; a chat channel will be opened and linked here.
- **Funding:** open-source grants and sponsors; possibly an optional hosted service later, which would never paywall core features.
- **License:** MIT ([LICENSE](LICENSE)).
