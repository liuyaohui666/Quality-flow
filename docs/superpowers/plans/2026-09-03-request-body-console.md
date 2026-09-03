# Editable Request Body Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let testers select a test type and registered suite, edit a suite-approved business JSON request body, submit it through the UI, and prove that Restful Booker tests execute with that exact data.

**Architecture:** Suite YAML owns a public JSON Schema and example. The API validates and stores request bodies separately from allowlisted runner parameters; the worker injects compact JSON into the trusted suite process without changing Celery messages or command arguments. The framework-free console performs friendly client-side parsing while the server remains authoritative.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, JSON Schema, SQLAlchemy 2, Alembic, pytest, HTML/CSS/JavaScript, Docker Compose.

## Global Constraints

- Preserve the existing registered-suite and `shell=False` trust boundary.
- Accept only JSON objects up to 64 KiB.
- Do not put request bodies in Outbox or Redis messages.
- Do not expose argv, working directories, arbitrary URLs, credentials, or filesystem paths.
- Keep existing callers valid when `request_body` is omitted.
- Use an Alembic migration for the nullable `runs.request_body` column.
- Do not add a Node.js build chain.

---

### Task 1: Suite-owned request-body contract

**Files:**
- Modify: `pyproject.toml`
- Modify: `config/suites.yaml`
- Modify: `src/quality_flow/suites/registry.py`
- Modify: `src/quality_flow/api/routes/suites.py`
- Modify: `src/quality_flow/api/schemas.py`
- Test: `tests/unit/test_suite_registry.py`
- Test: `tests/unit/test_api_console.py`

**Interfaces:**
- Produces: `RequestBodyDefinition(required, schema, example)`
- Produces: `SuiteDefinition.resolve_request_body(value) -> dict[str, Any] | None`
- Produces: safe catalog fields `test_type` and `request_body`

- [ ] Write tests proving Restful Booker accepts a valid object, rejects missing/extra/wrong-type fields, rejects bodies for suites without a contract, and exposes only the safe schema/example.
- [ ] Run the focused tests and confirm they fail before implementation.
- [ ] Add `jsonschema>=4.23,<5`, parse `test_type` plus `request_body` from YAML, cap compact UTF-8 JSON at 65,536 bytes, validate with `Draft202012Validator`, and return a defensive deep copy.
- [ ] Add the Restful Booker schema/example and API response models.
- [ ] Run focused tests and commit the contract.

### Task 2: Persist and expose the Run request body

**Files:**
- Modify: `src/quality_flow/application/run_service.py`
- Modify: `src/quality_flow/infrastructure/models.py`
- Modify: `src/quality_flow/infrastructure/database.py`
- Modify: `src/quality_flow/api/routes/runs.py`
- Modify: `src/quality_flow/api/schemas.py`
- Create: `migrations/versions/0003_run_request_body.py`
- Test: `tests/unit/test_run_service.py`
- Test: `tests/unit/test_api_runs.py`
- Test: `tests/integration/test_run_persistence.py`

**Interfaces:**
- Consumes: `SuiteDefinition.resolve_request_body`
- Produces: `RunService.create_run(..., request_body: dict[str, Any] | None = None)`
- Produces: nullable PostgreSQL `runs.request_body`
- Produces: `RunResponse.request_body`

- [ ] Write failing unit and integration tests for validation-before-transaction, persistence, idempotent replay, migration shape, and path-free API response.
- [ ] Add the nullable JSON column and thread the validated body through `NewRun`, UoW, ORM, API creation, and response mapping.
- [ ] Snapshot the public request-body contract with the suite while keeping the Outbox payload identifier-only.
- [ ] Run focused tests and commit persistence/API changes.

### Task 3: Deliver the body safely to Restful Booker

**Files:**
- Modify: `src/quality_flow/application/worker.py`
- Modify: `src/quality_flow/runners/base.py`
- Modify: `src/quality_flow/runners/subprocess_runner.py`
- Modify: `src/quality_flow/runners/pytest_runner.py`
- Modify: `src/quality_flow/runners/locust_runner.py`
- Modify: `demo_suites/restful_booker/conftest.py`
- Test: `tests/unit/test_subprocess_runner.py`
- Test: `tests/unit/test_pytest_runner.py`
- Test: `tests/unit/test_restful_booker_suite.py`
- Test: `tests/integration/test_worker_lifecycle.py`

**Interfaces:**
- Produces: `ExecutionSpec.request_body`
- Produces: `build_clean_environment(..., request_body=...)`
- Produces: `QUALITY_FLOW_REQUEST_BODY_JSON` inside the trusted subprocess only

- [ ] Write failing tests proving compact lossless JSON injection, no variable when absent, worker snapshot propagation, fixture override, and YAML fallback.
- [ ] Add immutable request-body data to claimed execution and execution spec.
- [ ] Serialize it with `json.dumps(..., separators=(",", ":"), ensure_ascii=False)` into the child environment; never append it to argv.
- [ ] Update Restful Booker `booking_data` to load the environment object as `valid_booking`, leaving other fixture data unchanged.
- [ ] Run focused tests and commit worker/suite integration.

### Task 4: Test-type and JSON-editor UI

**Files:**
- Modify: `src/quality_flow/api/static/index.html`
- Modify: `src/quality_flow/api/static/styles.css`
- Modify: `src/quality_flow/api/static/app.js`
- Test: `tests/unit/test_api_console.py`

**Interfaces:**
- Consumes: safe suite catalog and `POST /api/v1/runs`
- Produces: test-type filter, JSON editor, validate/format/example buttons, and request-body detail panel

- [ ] Write a failing UI contract test for all controls and JavaScript behavior markers.
- [ ] Add test-type-first selection and filter suites without allowing mismatched runner commands.
- [ ] Add an accessible monospace JSON editor with “加载示例”, “格式化”, and “校验 JSON” actions; hide it for suites without a body contract.
- [ ] Submit parsed `request_body`, surface client/server validation errors, and render the stored request body in Run details using `textContent`.
- [ ] Run route/JavaScript syntax tests and commit the UI.

### Task 5: Documentation and end-to-end verification

**Files:**
- Modify: `README.md`
- Modify: `docs/evidence-matrix.md`
- Modify: `tests/e2e/test_quality_flow.py`

**Interfaces:**
- Demonstrates: UI-created custom Restful Booker data and backward-compatible demo suites.

- [ ] Extend black-box E2E to assert a custom body survives POST/read and that invalid bodies return 422 without creating work.
- [ ] Document the four input layers, button-to-API mapping, safe JSON boundary, and no-production-secret warning.
- [ ] Run Ruff, all unit tests under Python 3.12 with init, PostgreSQL/Redis integration tests, Compose validation, E2E, JavaScript syntax, and diff/secret checks.
- [ ] Rebuild/start the real stack and use a browser to select API testing, edit JSON, validate, submit, observe terminal state, and inspect the stored body and artifacts.
- [ ] Commit documentation, merge locally to `main`, leave the validated console running, and do not push without explicit user instruction.

