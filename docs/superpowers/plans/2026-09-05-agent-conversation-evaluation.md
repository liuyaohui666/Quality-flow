# Agent Conversation Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add backward-compatible multi-turn Agent evaluation with ordered tool-trajectory checks and deterministic end-to-end proof.

**Architecture:** Normalize both legacy prompt cases and new conversation cases into immutable turns at definition-load time. Execute each conversation sequentially inside the existing Agent runner, aggregate per-turn evidence into one case result, and expose the nested trace through the existing artifact path.

**Tech Stack:** Python 3.12+, dataclasses, PyYAML, HTTPX, FastAPI/Pydantic, pytest, Docker Compose.

## Global Constraints

- Existing single-prompt Agent evaluation YAML must keep its current HTTP request contract.
- New multi-turn cases send full user/assistant message history on each turn.
- A conversation is one QualityFlow CaseResult and contains at most 20 turns.
- Definitions remain trusted, strict, immutable repository assets.
- No real model provider, semantic judge, tool execution, or credentials are added.

---

### Task 1: Parse immutable conversation definitions

**Files:**
- Modify: `src/quality_flow/runners/agent_eval_definition.py`
- Test: `tests/unit/test_agent_eval_definition.py`

**Interfaces:**
- Produces: `AgentEvalTurn(prompt: str, expectation: AgentExpectation)`.
- Produces: `AgentEvalCase.turns`, `AgentEvalCase.legacy_prompt`, `AgentEvalCase.expected_tool_sequence`, and `AgentEvalCase.max_total_tokens`.
- Consumes: the existing expectation parser and strict unknown-field validation.

- [ ] **Step 1: Write failing parser tests**

Add tests proving a two-turn case is normalized into immutable turns, legacy `prompt` remains supported, exactly one form is required, turn count is bounded, and invalid aggregate policies are rejected.

- [ ] **Step 2: Run the definition test module and observe failure**

Run: `python -m pytest tests/unit/test_agent_eval_definition.py -q`

Expected: failures because `turns`, `expected_tool_sequence`, and `max_total_tokens` are unknown.

- [ ] **Step 3: Implement strict parsing and normalization**

Add the immutable turn type, extend the allowed keys, reuse one `_parse_expectation` helper for legacy and multi-turn forms, and construct a one-turn tuple for legacy cases while retaining a boolean request-mode marker.

- [ ] **Step 4: Run definition tests**

Run: `python -m pytest tests/unit/test_agent_eval_definition.py -q`

Expected: all definition tests pass.

- [ ] **Step 5: Commit**

Commit message: `feat: define agent conversation evaluations`

### Task 2: Execute conversations and evaluate tool trajectories

**Files:**
- Modify: `src/quality_flow/runners/agent_eval_runner.py`
- Test: `tests/unit/test_agent_eval_runner.py`

**Interfaces:**
- Consumes: normalized `AgentEvalCase.turns` from Task 1.
- Sends: legacy `{ "prompt": str }` or conversation `{ "messages": [{"role": str, "content": str}] }`.
- Produces: one case result, nested turn reports, ordered aggregate tool validation, aggregate token validation, and `agent_turn_count`.

- [ ] **Step 1: Write failing runner tests**

Add tests that inspect the second request's complete history, verify a correct ordered tool trajectory passes, prove a reversed trajectory fails, prove total-token overflow fails, and preserve the existing legacy request assertion.

- [ ] **Step 2: Run the runner test module and observe failure**

Run: `python -m pytest tests/unit/test_agent_eval_runner.py -q`

Expected: failures because the runner still reads only `case.prompt` and emits no conversation trace.

- [ ] **Step 3: Implement minimal conversation execution**

Loop over normalized turns, render each prompt, build the correct request shape, append the assistant answer after structurally valid responses, retain per-turn evidence, aggregate tool names and tokens, and evaluate case-level rules after all turns.

- [ ] **Step 4: Preserve deadline and error behavior**

Stop the active case when a transport or validation error occurs, mark Run timeout only when the Run deadline is the limiting timeout, and leave untouched cases skipped.

- [ ] **Step 5: Run runner and focused Agent tests**

Run: `python -m pytest tests/unit/test_agent_eval_runner.py tests/unit/test_agent_eval_definition.py tests/unit/test_runner_metrics.py -q`

Expected: all focused tests pass.

- [ ] **Step 6: Commit**

Commit message: `feat: evaluate multi-turn agent traces`

### Task 3: Demonstrate and document the feature end to end

**Files:**
- Modify: `demo_target/app.py`
- Modify: `demo_suites/agent_eval/safety.yaml`
- Modify: `tests/unit/test_demo_target.py`
- Modify: `tests/e2e/test_quality_flow.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `messages` requests created by Task 2.
- Produces: deterministic context-memory, ordered-tool, and injection-refusal conversations.

- [ ] **Step 1: Write failing target and E2E contract tests**

Add target tests for legacy prompt compatibility and history-aware responses. Update E2E expectations to require six executed turns, a passing ordered trajectory, and nested two-turn reports.

- [ ] **Step 2: Run target tests and observe failure**

Run: `python -m pytest tests/unit/test_demo_target.py tests/unit/test_delivery_contracts.py -q`

Expected: the messages payload is rejected until the target contract is extended.

- [ ] **Step 3: Extend the deterministic target and registered suite**

Accept exactly one of `prompt` or `messages`, derive the latest user request and full conversation text, and return fixed structured behaviors for the three registered conversations.

- [ ] **Step 4: Update user documentation**

Explain the new context, trace, report, and metric behavior in plain language while preserving the explicit deterministic-demo limitation.

- [ ] **Step 5: Run static and unit verification**

Run: `python -m ruff check .`

Run: `python -m pytest tests/unit -q`

Expected: lint passes and all unit tests pass.

- [ ] **Step 6: Run real integration and E2E verification**

Use the repository's Compose-backed CI commands to run PostgreSQL/Redis integration tests and the complete platform E2E suite.

Expected: integration and E2E suites pass, including the multi-turn Agent evaluation.

- [ ] **Step 7: Commit**

Commit message: `test: prove agent conversations end to end`

### Task 4: Merge, push, and inspect hosted CI

**Files:**
- No source changes expected.

**Interfaces:**
- Consumes: verified feature branch.
- Produces: updated `main` on `origin` plus hosted CI evidence.

- [ ] **Step 1: Merge the feature branch into local main**

Use a non-interactive fast-forward or normal merge without rewriting user history.

- [ ] **Step 2: Re-run the full unit gate on merged main**

Run: `python -m ruff check .`

Run: `python -m pytest tests/unit -q`

Expected: both commands pass on the merged commit.

- [ ] **Step 3: Push main and inspect GitHub Actions**

Push `main` to `origin`, then verify the `quality`, `integration`, and `e2e` jobs reach a successful conclusion. If hosted status cannot be read because of tooling or API limits, report that limitation separately from local verification.
