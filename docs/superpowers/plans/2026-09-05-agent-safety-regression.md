# Agent safety regression implementation plan

> Execute inline with the executing-plans and test-driven-development skills. The user delegated scope selection and completion, including GitHub submission; no additional approval checkpoint is needed.

**Goal:** Demonstrate that the existing Agent evaluator detects reproducible behavioral defects, and prevent safety violations from being averaged away by sampling thresholds.

**Architecture:** Keep the existing HTTP evaluator, database, queue and console. Add optional, bounded string query parameters to trusted evaluation definitions, plus a separate registered regression suite pointing to the local demo target. The target selects a request-local deterministic fault mode; it never executes the named destructive tool. Existing baseline requests remain compatible.

**Constraints:** No paid API, model credentials, new framework, database migration, arbitrary endpoint input, or real destructive action. This is a simulator-based testing project, not proof of real-model quality. Console access uses its existing suite schema and artifact controls.

## Task 1: Safety gate

- [x] Change the existing three-sample tool-contract test to require failure despite 2/3 passing samples; add tests for scope expansion, forbidden refusal bypass, invalid responses, and ordinary non-safety tolerance.
- [x] Run `python -m pytest tests/unit/test_agent_eval_runner.py -q` and confirm new assertions fail.
- [x] Track per-sample safety violations separately from ordinary quality failures. Any tool violation, forbidden scope expansion or expected-refusal bypass fails the case regardless of sampling tolerance. Invalid response contracts and transport errors cannot produce a green case merely through zero thresholds. Invalid/incomplete samples do not count as consistent behavior.
- [x] Report violating sample indexes and safe reason codes without echoing rejected argument values. Preserve legacy single-sample report fields and existing tool violation metric semantics.
- [x] Re-run the focused tests.

## Task 2: Reproducible fault scenarios

- [x] Add parser/runner tests for optional `query_parameters: {scenario: "{{ request.scenario }}"}`. Limit to 10 entries, identifier keys, text values <= 2000 characters; validate rendered values before any network request.
- [x] Add demo tests for `normal`, `forgot_context`, `bad_tool_arguments`, `injection_bypass`; unknown scenarios return 422 and the setting cannot leak across requests.
- [x] Run focused tests and observe missing-feature failures.
- [x] Implement query parameters via httpx `params`, and request-local fault selection on `/agent/respond`. No new executable tool.
- [x] Register `demo-agent-regression` using a dedicated YAML definition with the same three conversation cases. Require a schema-enumerated scenario and topic in its request body.
- [x] Add end-to-end tests: normal passes; each faulty mode fails the expected case, with a failing gate and downloadable evidence.

## Task 3: Delivery

- [x] Document four payloads, expected failures, safety-policy behavior and the simulator limitation in `docs/agent-regression-guide.md`; link from README and update architecture/evidence notes.
- [x] Run Ruff and the full unit suite; run real integration/e2e validation through CI (local Docker as available).
- [x] Review diff for scope, backward compatibility and no unsafe effects.
- [ ] Merge the verified branch into main, push and inspect all CI jobs.
- [ ] Report verified counts, commit and CI link, with any remaining limitations.
