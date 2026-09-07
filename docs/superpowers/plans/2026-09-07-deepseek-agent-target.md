# DeepSeek Agent Target Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a secure, bounded DeepSeek-backed Agent target that the existing QualityFlow Agent Eval Runner can test manually.

**Architecture:** Keep the deterministic target for CI and add a separate model-backed endpoint. A focused provider module owns DeepSeek HTTP transport, tool-loop orchestration, argument validation, safe local tools, error translation, and the existing Agent response contract.

**Tech Stack:** Python 3.12+, FastAPI, Pydantic, HTTPX, JSON Schema, Docker Compose, pytest

## Global Constraints

- Never commit or log an API key.
- Real DeepSeek calls are manual only and excluded from CI.
- Default model is `deepseek-v4-flash`, configurable by environment.
- Only local side-effect-free tools may execute.
- At most three model/tool rounds per request.
- Existing deterministic Agent behavior and tests must remain compatible.

---

### Task 1: DeepSeek provider and bounded tool loop

**Files:**
- Create: `demo_target/deepseek_agent.py`
- Test: `tests/unit/test_deepseek_agent.py`

**Interfaces:**
- Consumes: OpenAI-compatible `POST /chat/completions` responses and existing `AgentMessage`-shaped history.
- Produces: `DeepSeekAgent.respond(messages: Sequence[Mapping[str, str]]) -> AgentResult` and stable provider exceptions.

- [ ] **Step 1: Write failing tests for plain answers, valid tool loops, invalid tool arguments, round limits, missing keys, provider failures, and secret-safe errors.**
- [ ] **Step 2: Run `python -m pytest tests/unit/test_deepseek_agent.py -q` and verify failures are caused by the missing implementation.**
- [ ] **Step 3: Implement immutable result types, HTTP client injection, DeepSeek request/response parsing, JSON Schema validation, safe tools, and the bounded loop.**
- [ ] **Step 4: Run `python -m pytest tests/unit/test_deepseek_agent.py -q` and verify all provider tests pass.**

### Task 2: FastAPI endpoint and configuration

**Files:**
- Modify: `demo_target/app.py`
- Modify: `tests/unit/test_demo_target.py`

**Interfaces:**
- Consumes: optional injected `DeepSeekAgent` and `DEEPSEEK_*` environment settings.
- Produces: `POST /agent/deepseek/respond` with the existing structured Agent response contract.

- [ ] **Step 1: Write failing endpoint tests for successful delegation, multi-turn forwarding, missing configuration, and sanitized provider failure mapping.**
- [ ] **Step 2: Run the focused endpoint tests and confirm the new route is absent.**
- [ ] **Step 3: Add dependency construction and the new route without changing `/agent/respond`.**
- [ ] **Step 4: Run the focused target tests and verify they pass.**

### Task 3: Manual suite, Compose secret plumbing, and operator documentation

**Files:**
- Create: `demo_suites/agent_eval/deepseek.yaml`
- Modify: `config/suites.yaml`
- Modify: `compose.yaml`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `tests/unit/test_suite_registry.py`
- Modify: `tests/unit/test_compose_contract.py`

**Interfaces:**
- Consumes: local `DEEPSEEK_API_KEY`, optional base URL/model/timeout environment settings.
- Produces: a console-visible `deepseek-agent-eval` suite that reaches the real endpoint while deterministic CI remains unchanged.

- [ ] **Step 1: Write failing registry and Compose contract tests for the real suite and demo-target-only secret injection.**
- [ ] **Step 2: Run the focused tests and verify the expected configuration is missing.**
- [ ] **Step 3: Add the suite, environment wiring, placeholder documentation, safe key-entry instructions, and cost warning.**
- [ ] **Step 4: Run the focused tests and verify they pass.**

### Task 4: Full verification and delivery

**Files:**
- Review: all changed files

**Interfaces:**
- Consumes: completed Tasks 1-3.
- Produces: verified, committed, pushed implementation with observed CI status.

- [ ] **Step 1: Run Ruff, the complete unit suite, configuration/security scans, and relevant integration/e2e checks.**
- [ ] **Step 2: Inspect the diff for key leakage, accidental paid calls, contract drift, and unrelated changes.**
- [ ] **Step 3: Commit and push the verified changes to `main`.**
- [ ] **Step 4: Observe the GitHub Actions run and report exact results.**
