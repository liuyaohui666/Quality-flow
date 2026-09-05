# Agent Stability Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded multi-sample correctness and behavioral-consistency evaluation to the existing Agent runner.

**Architecture:** Extend the immutable YAML definition with sampling policy, refactor one conversation execution into a sample-level outcome, then aggregate samples into the existing case-level result. Preserve one-sample compatibility while exposing sample evidence and aggregate metrics.

**Tech Stack:** Python 3.14, dataclasses, PyYAML, httpx, pytest, Docker Compose, GitHub Actions.

## Global Constraints

- `sample_count` is an integer from 1 through 10 and defaults to 1.
- Both threshold values are finite numbers from 0 through 1 and default to 1.0.
- A definition may schedule at most 500 HTTP turns.
- Stability compares decision and tool trajectories, not answer wording.
- Existing one-sample definitions and report fields remain compatible.
- The overall Run deadline overrides all sampling work.

---

### Task 1: Parse bounded sampling policies

**Files:**
- Modify: `src/quality_flow/runners/agent_eval_definition.py`
- Test: `tests/unit/test_agent_eval_definition.py`

**Interfaces:**
- Consumes: existing case and turn YAML structures.
- Produces: `AgentEvalCase.sample_count`, `min_sample_pass_rate`, and `min_behavior_consistency_rate`.

- [ ] Write tests that assert explicit fields, defaults, type/range rejection, and the 500-turn definition budget.
- [ ] Run `python -m pytest tests/unit/test_agent_eval_definition.py -q` and verify the new tests fail because sampling fields are unsupported.
- [ ] Implement strict parsing and total planned-turn validation.
- [ ] Run the definition tests and verify they pass.
- [ ] Commit the parser and tests.

### Task 2: Execute and aggregate independent samples

**Files:**
- Modify: `src/quality_flow/runners/agent_eval_runner.py`
- Test: `tests/unit/test_agent_eval_runner.py`

**Interfaces:**
- Consumes: sampling fields on `AgentEvalCase`.
- Produces: independent sample reports, case-level rates, and the three new metrics.

- [ ] Write tests for fresh history, allowed partial pass rate, behavior inconsistency, compatibility, metrics, and deadline exhaustion.
- [ ] Run `python -m pytest tests/unit/test_agent_eval_runner.py -q` and verify the new tests fail for missing sampling behavior.
- [ ] Extract one conversation into a sample outcome and aggregate samples in `_run_case`.
- [ ] Add behavior signatures, threshold messages, report fields, and metrics.
- [ ] Run Agent definition and runner tests and verify they pass.
- [ ] Commit runner behavior and tests.

### Task 3: Demonstrate and document stability evaluation

**Files:**
- Modify: `demo_suites/agent_eval/safety.yaml`
- Modify: `tests/unit/test_agent_eval_definition.py`
- Modify: `tests/e2e/test_quality_flow.py`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/evidence-matrix.md`

**Interfaces:**
- Consumes: multi-sample Agent evaluation support.
- Produces: a deterministic demo with five samples and ten turns plus persisted E2E evidence.

- [ ] Update the E2E assertions first and confirm they fail against the old demo configuration.
- [ ] Configure context-memory for three samples at 100% pass and consistency thresholds.
- [ ] Update user and architecture documentation with the distinction between correctness and stability.
- [ ] Run targeted unit and E2E tests.
- [ ] Commit the demo, E2E proof, and documentation.

### Task 4: Verify, integrate, publish, and inspect CI

**Files:**
- Verify all changed files and repository workflows.

**Interfaces:**
- Consumes: completed feature commits.
- Produces: verified `main`, pushed GitHub commit, and green `quality`, `integration`, and `e2e` jobs.

- [ ] Run `python -m ruff check .`.
- [ ] Run `python -m pytest tests/unit -q`.
- [ ] Run the Docker-backed integration suite.
- [ ] Run the Docker-backed end-to-end suite.
- [ ] Fast-forward merge the feature branch into `main` and rerun local lint and unit tests.
- [ ] Push `main`, inspect the GitHub Actions run, and report every job's actual result.
- [ ] Remove the temporary Docker project and owned worktree only after successful integration.
