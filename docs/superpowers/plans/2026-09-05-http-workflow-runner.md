# HTTP Workflow Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a registered, declarative HTTP workflow runner that passes values between dependent API steps and publishes step results through QualityFlow's existing Run model.

**Architecture:** Parse trusted YAML into immutable workflow definitions, execute the definitions in-process with a bounded `httpx.Client`, and translate every step into existing `CaseResultData`, gate, and artifact types. Register the new adapter through the existing suite registry and Celery worker factory, then demonstrate it against deterministic local resource endpoints.

**Tech Stack:** Python 3.12+, PyYAML, httpx, jsonschema, FastAPI, pytest, existing QualityFlow runner and artifact contracts.

## Global Constraints

- API users cannot provide workflow definitions, URLs, or commands.
- Workflow definition paths must resolve inside the copied attempt workspace.
- Only `GET`, `POST`, `PUT`, `PATCH`, and `DELETE` are accepted.
- Template namespaces are limited to Run request body, allowlisted parameters, explicit worker environment, and captured values.
- Reports redact sensitive keys and cap recorded bodies.
- No database migration or new frontend framework is introduced.

---

### Task 1: Immutable Workflow Definition and Templates

**Files:**
- Create: `src/quality_flow/runners/workflow_definition.py`
- Test: `tests/unit/test_workflow_definition.py`

**Interfaces:**
- Produces: `WorkflowDefinition.from_yaml(path: Path) -> WorkflowDefinition`
- Produces: `render_template(value: Any, context: Mapping[str, Any]) -> Any`
- Produces: `extract_json_path(value: Any, path: str) -> Any`

- [ ] Write tests for valid parsing, rejected unknown fields/methods/duplicate ids, safe template rendering, missing variables, and nested capture paths.
- [ ] Run `python -m pytest tests/unit/test_workflow_definition.py -q` and confirm failure because the module does not exist.
- [ ] Implement frozen definition dataclasses, strict YAML validation, the four allowed template namespaces, and restricted JSON-path capture.
- [ ] Run the focused tests and confirm they pass.
- [ ] Commit definition parsing and tests.

### Task 2: Workflow Execution Adapter

**Files:**
- Create: `src/quality_flow/runners/workflow_runner.py`
- Test: `tests/unit/test_workflow_runner.py`

**Interfaces:**
- Consumes: `WorkflowDefinition`, `render_template`, `extract_json_path`
- Produces: `WorkflowRunner.run(spec, workspace, heartbeat) -> RunnerOutcome`

- [ ] Write tests using `httpx.MockTransport` for capture-to-next-step, assertion failure with later-step skipping, cleanup after failure, missing cleanup variables, schema assertion, transport error, timeout, invalid definition, and report redaction.
- [ ] Run `python -m pytest tests/unit/test_workflow_runner.py -q` and confirm failure because the adapter does not exist.
- [ ] Implement exact `workflow <relative-yaml-path>` argv validation, deadline-aware requests, step evaluation, cleanup execution, functional gate evaluation, and a sanitized `workflow_report` artifact.
- [ ] Run the focused tests and confirm they pass.
- [ ] Commit the runner and tests.

### Task 3: Registry and Worker Wiring

**Files:**
- Modify: `src/quality_flow/suites/registry.py`
- Modify: `src/quality_flow/worker/tasks.py`
- Modify: `tests/unit/test_suite_registry.py`
- Modify: `tests/unit/test_delivery_contracts.py`

**Interfaces:**
- Extends: `SuiteDefinition.runner_type` with `workflow`
- Registers: `runners["workflow"] = WorkflowRunner(...)`

- [ ] Add failing registry and worker-construction tests for the new runner type.
- [ ] Run focused tests and confirm the new expectations fail.
- [ ] Extend the registry allowlist and register `WorkflowRunner` with only `QUALITY_FLOW_TARGET_URL` exposed.
- [ ] Run focused tests and confirm they pass.
- [ ] Commit runner wiring.

### Task 4: Deterministic Workflow Demo

**Files:**
- Modify: `demo_target/app.py`
- Create: `demo_suites/workflow/resource_lifecycle.yaml`
- Modify: `config/suites.yaml`
- Modify: `tests/unit/test_demo_target.py`
- Modify: `tests/unit/test_suite_registry.py`

**Interfaces:**
- Adds: `/workflow/resources` create endpoint
- Adds: `/workflow/resources/{resource_id}` read, update, and delete endpoints
- Registers: suite id `demo-workflow`

- [ ] Add failing endpoint and suite-registration tests.
- [ ] Run focused tests and confirm failure for the missing resource endpoints and suite.
- [ ] Implement an app-scoped in-memory resource store and the create/read/update/delete endpoints.
- [ ] Add the lifecycle workflow and a request-body schema with a safe example.
- [ ] Run focused tests and confirm they pass.
- [ ] Commit the deterministic demo.

### Task 5: Documentation and Whole-Project Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/evidence-matrix.md`
- Test: `tests/unit`

**Interfaces:**
- Documents: how to submit and inspect `demo-workflow`
- Documents: dependent-step data flow and current boundaries

- [ ] Document the workflow format, execution path, example submission, and difference between workflow failure and infrastructure failure.
- [ ] Run `python -m ruff check .`.
- [ ] Run `python -m pytest tests/unit -q`.
- [ ] Run Docker-backed integration and end-to-end checks when Docker is available.
- [ ] Review `git diff --check` and the requirements in the design spec.
- [ ] Commit documentation and verified evidence.
