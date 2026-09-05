# Agent Tool Argument Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate Agent tool arguments and call counts against trusted per-turn contracts.

**Architecture:** Parse and validate optional Draft 2020-12 schemas into immutable Agent expectations, then apply them during the runner's existing structured-response evaluation. Reuse the current tool-policy failure path and metric instead of creating a second competing policy system.

**Tech Stack:** Python 3.14, PyYAML, jsonschema, httpx, pytest, Docker Compose, GitHub Actions.

## Global Constraints

- Only schemas in pre-registered repository YAML are trusted.
- Schema keys must also be present in `allowed_tools`.
- Invalid schemas fail definition loading, not test execution.
- Runtime failures never echo rejected argument values in messages.
- Existing Agent definitions remain compatible.

---

### Task 1: Parse trusted tool contracts

**Files:**
- Modify: `src/quality_flow/runners/agent_eval_definition.py`
- Test: `tests/unit/test_agent_eval_definition.py`

**Interfaces:**
- Consumes: optional expectation fields `tool_argument_schemas` and `max_tool_calls`.
- Produces: read-only `AgentExpectation.tool_argument_schemas` and validated `max_tool_calls`.

- [ ] Write failing tests for valid contracts, defaults, invalid schemas, unknown tool keys, bounds, and top-level immutability.
- [ ] Run the definition tests and confirm the failures are caused by unsupported fields.
- [ ] Implement strict Draft 2020-12 schema checks and parsing.
- [ ] Run definition tests and Ruff.
- [ ] Commit the parser and tests.

### Task 2: Enforce arguments and call-count policy

**Files:**
- Modify: `src/quality_flow/runners/agent_eval_runner.py`
- Test: `tests/unit/test_agent_eval_runner.py`

**Interfaces:**
- Consumes: parsed tool contracts and structured `tool_calls`.
- Produces: safe rule failures included in turn status, case status, sampling, and `tool_violation_rate`.

- [ ] Write failing tests for valid arguments, missing/wrong/extra fields, excessive calls, non-disclosure, and sampling interaction.
- [ ] Run the runner tests and confirm expected failures.
- [ ] Validate each matching call with `Draft202012Validator` and add safe path/keyword messages.
- [ ] Run Agent tests and Ruff.
- [ ] Commit runner behavior and tests.

### Task 3: Demonstrate, document, and prove end to end

**Files:**
- Modify: `demo_suites/agent_eval/safety.yaml`
- Modify: `tests/unit/test_agent_eval_definition.py`
- Modify: `tests/e2e/test_quality_flow.py`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/evidence-matrix.md`

**Interfaces:**
- Consumes: tool argument contract support.
- Produces: strict weather/calendar demo contracts and persisted zero-violation evidence.

- [ ] Add contract assertions before changing the demo and verify the unit test fails.
- [ ] Add schemas and one-call limits to both tool turns.
- [ ] Update documentation and evidence claims without removing locked wording.
- [ ] Run targeted tests and commit.

### Task 4: Verify and publish

**Files:**
- Verify the repository and hosted workflow.

**Interfaces:**
- Consumes: completed feature commits.
- Produces: clean `main`, pushed commit, and green quality/integration/e2e jobs.

- [ ] Run Ruff and all unit tests.
- [ ] Run real PostgreSQL/Redis integration tests.
- [ ] Run the full Compose E2E suite.
- [ ] Fast-forward merge to `main`, rerun local checks, and push.
- [ ] Inspect GitHub Actions job results and clean owned temporary resources.
