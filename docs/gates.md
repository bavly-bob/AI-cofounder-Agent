# Gates in detail

Key facts: every gate states its target before the step runs (logged as `set_gate_target`); missing or unsourced evidence is always STOP; the system only recommends and never upgrades a STOP; the founder confirms or overrides, and both outcomes are logged. Thresholds are in `config/thresholds.json`. Code: `gates.py`.

## Gate types

| Gate | Used after | Rule |
|---|---|---|
| `Phase1EvidenceGate` (`discover`) | step 1.4 | see below |
| `FounderApprovalGate` | steps 2.1, 2.2, 2.3 | asks for one publish / send / spend / sign action through `approval.guarded_action`. Approved and executed: GO. Explicit No: STOP, or REPEAT where the step allows it (canvas, target). No answer: STOP (denied by default). Not reviewable: a denied action cannot become GO at a gate prompt. |
| `Phase2DemandGate` (`validate`) | step 2.4 | see below |
| `EvidenceThresholdGate` | generic | one metric against a fixed target: met = GO; 50-99% = REPEAT while attempts remain, then PIVOT; below 50% = STOP. A MOCKED metric is labelled on every outcome. |
| `SourcedClaimsGate` | generic | share of claims with a source URL, same bands |
| `TestsPassGate` | generic (designed for Phase 3) | a check function passes: GO; fails: REPEAT while attempts remain, then STOP |
| `StubPassThroughGate` | engine tests | lets only a STUBBED step through, labelled as a stub; any real step is STOP |

## Phase 1 evidence gate

Counts only sourced (retrieved URL), non-MOCKED pain signals; several signals from one page count once; interview notes are not counted.

- **STOP** if: fewer than 3 sourced signals or fewer than 2 distinct domains; main score below 2.5/5; a hard founder-fit blocker; an excluded product.
- **PIVOT** if an alternative scores at least 0.5 higher and has at least 2 sourced signals of its own: Phase 1 reruns on that alternative (at most one pivot per run).
- **REPEAT** step 1.1 once with new queries if the evidence is borderline (4 or fewer signals, or a score of 3.0 or less).
- **GO** otherwise, with the open risks listed (web text is not customer conversations; real interviews are needed).

## Phase 2 demand gate

Judges the evidence of step 2.4 against the target the founder approved in step 2.2.

- **STOP** if: no target was approved before results; the stored target no longer matches its SHA-256 hash, or differs from the target written to the decision log when the founder approved it (`register_experiment_target`); the `--waitlist` export could not be read (no other numbers are used instead); no sign-up data was supplied ("demand test not run yet", with the `--resume` command to rerun).
- **GO** if sign-ups reach the target and conversion reaches the minimum (default 100 sign-ups in 14 days, 5%).
- **REPEAT** once (a new experiment from step 2.2, approved again; the count is saved with the run, so a `--resume` does not grant a second REPEAT) if sign-ups are 50-99% of the target, or the target is met but conversion is low or not measured; after that, **PIVOT**.
- Below 50%: **PIVOT** if Phase 1's best alternative scored at least 3.5/5 with 2 sourced signals, otherwise **STOP**.
- A Phase 2 PIVOT halts the run with a memo that names the alternative; a new run starts on it.
- MOCKED numbers (`--mock-signups`) add "MOCKED evidence — demonstration only ... not a real validation" to every outcome.

## Founder review

At the CLI the founder types `accept`, or overrides with `go`, `repeat`, `pivot` or `stop`; Enter, or a non-interactive input, records no founder decision and the recommendation stands. In the app the gate card offers Approve recommendation, Reject (halts the run) and Override, which needs a written reason. Each answer is logged with `approved_by: "founder"`; an override is logged with both the system and the founder outcome.

## Limits and stop memo

The engine runs a step at most three times (a third REPEAT becomes PIVOT) and pivots at most once per run. A halted run writes `phase<N>_gate_stop_memo.md`: the reasons, the evidence, a compliant alternative where one applies, and what evidence would change the decision. Refusals by the guardrails are STOP and cannot be overridden at a gate.
