# Restful Booker Suite Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register and execute the existing Restful Booker pytest API regression as a trusted external QualityFlow suite without making the public service a required CI dependency.

**Architecture:** A selectively adapted copy of revision `387568c` lives under `demo_suites/restful_booker`. The existing Registry, Worker and PytestRunner execute it unchanged through their normal snapshot, workspace, JUnit, gate and Artifact path. Push/PR CI proves the suite offline; one explicit live run proves the external API boundary.

**Tech Stack:** Python 3.12, pytest 8, requests 2, PyYAML 6, QualityFlow SuiteRegistry/PytestRunner, Docker Compose, GitHub Actions.

## Global Constraints

- Do not modify or clean `D:\New_project`; it contains user-owned uncommitted changes.
- Import only source material from committed Restful Booker revision `387568c`, then apply intentional QualityFlow adaptations.
- Never persist a raw auth token in logs, JUnit, stdout/stderr, Allure or Git.
- Do not pass credentials or arbitrary URLs through Run parameters.
- Do not make public Restful Booker availability a required push/PR CI gate.
- Preserve the existing deterministic `demo_target` integration/e2e path.

---

### Task 1: Lock the registry and dependency contract

**Files:**
- Modify: `tests/unit/test_suite_registry.py`
- Modify: `tests/unit/test_delivery_contracts.py`
- Modify: `config/suites.yaml`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `SuiteRegistry.from_yaml(path, project_root)` and `SuiteDefinition.resolve_parameters()`.
- Produces: registered suite `restful-booker-api` and runtime dependency `requests>=2.31,<3`.

- [ ] **Step 1: Write the failing registry test**

Add a test that loads the repository registry and asserts:

```python
suite = registry.get("restful-booker-api")
assert suite.runner_type == "pytest"
assert suite.working_directory == (
    Path(__file__).resolve().parents[2] / "demo_suites" / "restful_booker"
).resolve()
assert suite.argv == (
    "python", "-m", "pytest", "tests/test_booking_crud.py", "-q", "--strict-markers"
)
assert suite.timeout_seconds == 120
assert suite.resolve_parameters({}) == {}
with pytest.raises(InvalidSuiteParameter):
    suite.resolve_parameters({"base_url": "https://example.test"})
assert suite.gate_policy.min_pass_rate == 1.0
assert suite.gate_policy.max_failures == 0
```

- [ ] **Step 2: Write the failing delivery test**

Parse `pyproject.toml` and the workflow, then assert `requests>=2.31,<3` is a project dependency and the required e2e commands do not contain `restful-booker-api` or `restful-booker.herokuapp.com`.

- [ ] **Step 3: Verify RED**

Run:

```powershell
python -m pytest tests/unit/test_suite_registry.py tests/unit/test_delivery_contracts.py -q
```

Expected: failures because the suite and requests dependency do not exist.

- [ ] **Step 4: Add the minimal registration and dependency**

Add this suite definition to `config/suites.yaml`:

```yaml
  restful-booker-api:
    runner_type: pytest
    working_directory: demo_suites/restful_booker
    argv:
      - python
      - -m
      - pytest
      - tests/test_booking_crud.py
      - -q
      - --strict-markers
    timeout_seconds: 120
    allowed_parameters: {}
    gate_policy:
      min_pass_rate: 1.0
      max_failures: 0
    source_revision: restful-booker@387568c
```

Add `requests>=2.31,<3` to `[project].dependencies`.

- [ ] **Step 5: Verify GREEN**

Run the same two test files. Expected: the new contract assertions pass; a missing suite directory is allowed until Task 2 because registry parsing does not execute it.

### Task 2: Import the suite with token-safe logging

**Files:**
- Create: `demo_suites/restful_booker/pytest.ini`
- Create: `demo_suites/restful_booker/conftest.py`
- Create: `demo_suites/restful_booker/clients/__init__.py`
- Create: `demo_suites/restful_booker/clients/booking_client.py`
- Create: `demo_suites/restful_booker/config/config.yaml`
- Create: `demo_suites/restful_booker/data/booking_data.yaml`
- Create: `demo_suites/restful_booker/utils/__init__.py`
- Create: `demo_suites/restful_booker/utils/assertions.py`
- Create: `demo_suites/restful_booker/utils/config_loader.py`
- Create: `demo_suites/restful_booker/utils/logger.py`
- Create: `demo_suites/restful_booker/tests/test_assertions.py`
- Create: `demo_suites/restful_booker/tests/test_booking_client.py`
- Create: `demo_suites/restful_booker/tests/test_config.py`
- Create: `demo_suites/restful_booker/tests/test_logger.py`
- Create: `demo_suites/restful_booker/tests/test_booking_crud.py`

**Interfaces:**
- Consumes: fixed public configuration and pytest fixtures.
- Produces: `BookingClient`, token-safe request/response logs, seven CRUD cases and offline unit tests.

- [ ] **Step 1: Write the offline tests first**

Port the committed client/config/assertion/logger tests. Extend the client test with an injected in-memory logger:

```python
client.create_token({"username": "admin", "password": "do-not-log"})
logged = stream.getvalue()
assert "do-not-log" not in logged
assert "raw-auth-token" not in logged
assert '"token": "***"' in logged
```

The fake response returns `{"token": "raw-auth-token"}` and the test also asserts the original response object still returns the raw token to its caller.

- [ ] **Step 2: Verify RED**

Run from `demo_suites/restful_booker`:

```powershell
python -m pytest tests/test_booking_client.py tests/test_assertions.py tests/test_config.py tests/test_logger.py -q
```

Expected: import/collection errors because the suite implementation has not been created.

- [ ] **Step 3: Implement the minimal adapted suite**

Port the API client, fixture, YAML loader, assertions and logger. Remove Allure imports/decorators/attachments. Before logging, recursively replace values whose case-folded keys are `password`, `token`, `authorization`, `cookie` or `set-cookie` with `***`. Preserve the original `Response` so `auth_token` can still read the real token.

Use the public configuration:

```yaml
base_url: https://restful-booker.herokuapp.com
request_timeout: 15
auth:
  username: admin
  password: password123
```

Keep the seven CRUD cases, function-scoped create/cleanup fixture and strict `api`/`smoke` markers.

- [ ] **Step 4: Verify GREEN**

Run the four offline test files. Expected: all pass without network access and the token/password assertions pass.

### Task 3: Prove the suite through the real QualityFlow runner

**Files:**
- Create: `tests/unit/test_restful_booker_suite.py`

**Interfaces:**
- Consumes: repository `SuiteRegistry`, `ExecutionSpec`, `PytestRunner`, copied suite directory.
- Produces: regression proof for isolated workspace execution, JUnit parsing, functional gate and platform Artifacts.

- [ ] **Step 1: Write the failing runner-contract test**

Copy `demo_suites/restful_booker` to `tmp_path/workspace`, construct an offline `ExecutionSpec` targeting the four unit files, and execute `PytestRunner(staging_root=tmp_path / "staging")`. Assert:

```python
assert outcome.attempt_status is AttemptStatus.PASSED
assert outcome.case_summary is not None
assert outcome.case_summary.total >= 8
assert outcome.case_summary.failed == 0
assert outcome.case_summary.errors == 0
assert outcome.gate_result is not None and outcome.gate_result.passed
assert {item.artifact_type for item in outcome.artifacts} == {
    "stdout", "stderr", "junit_xml"
}
```

- [ ] **Step 2: Verify RED**

Run:

```powershell
python -m pytest tests/unit/test_restful_booker_suite.py -q
```

Expected: failure until Task 2 files and the new dependency are available in the QualityFlow environment.

- [ ] **Step 3: Make only contract-level fixes**

Fix import paths, test isolation or logger injection only when the real runner test exposes them. Do not add retries or network behavior.

- [ ] **Step 4: Verify GREEN and regression**

Run:

```powershell
python -m pytest tests/unit/test_restful_booker_suite.py tests/unit/test_suite_registry.py -q
python -m pytest tests/unit -q
python -m ruff check .
python -m pip check
```

Expected: all unit tests and static checks pass.

### Task 4: Document the external-suite boundary and live command

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/evidence-matrix.md`
- Modify: `tests/unit/test_delivery_contracts.py`

**Interfaces:**
- Consumes: public Run API and the registered suite.
- Produces: truthful operator instructions and evidence boundaries.

- [ ] **Step 1: Add a failing documentation contract**

Assert the documents contain `restful-booker-api`, `parameters: {}`, `公开外部服务`, `不进入必跑 CI`, `JUnit/stdout/stderr`, and do not claim Allure is archived by QualityFlow.

- [ ] **Step 2: Verify RED**

Run the delivery contract file and observe the new assertions fail.

- [ ] **Step 3: Add exact usage documentation**

Document this request:

```json
{
  "suite_id": "restful-booker-api",
  "parameters": {}
}
```

Explain that the resulting Run tests the public Restful Booker deployment; the target source is not in this repository; public availability is not a required CI gate; QualityFlow archives JUnit/stdout/stderr while the standalone project retains Allure.

- [ ] **Step 4: Verify GREEN**

Run the delivery contract file and `git diff --check`.

### Task 5: Validate live execution, commit, push and monitor

**Files:**
- Modify only if a verified defect is found; every defect first receives a failing regression test.

**Interfaces:**
- Consumes: local Python, public Restful Booker API, Docker Compose stack and GitHub Actions.
- Produces: separate live external evidence plus a green hosted QualityFlow revision.

- [ ] **Step 1: Run the direct public regression**

From `demo_suites/restful_booker` run:

```powershell
python -m pytest tests/test_booking_crud.py -q --strict-markers
```

Expected when the public demo is healthy: `7 passed`. If unavailable, retain the exact network/status evidence and do not weaken tests.

- [ ] **Step 2: Run a QualityFlow-managed public Run**

Build/start the named Compose project, POST the registered suite with empty parameters, poll `GET /api/v1/runs/{run_id}`, and assert `completed/passed`, seven cases, a passed gate and three platform Artifact types. Tear down only that named project.

- [ ] **Step 3: Run final local gates**

Run unit, isolated PostgreSQL/Redis integration, deterministic Compose E2E, Ruff, pip check, compileall and `git diff --check`. Record exact counts and external/public-service status separately.

- [ ] **Step 4: Commit and push intentionally**

Stage only the design, plan, suite, registry, dependency, tests and docs. Commit with `feat: integrate Restful Booker test suite`, push `main`, and verify local/remote SHA equality and a clean working tree.

- [ ] **Step 5: Monitor hosted CI until terminal**

Inspect the latest GitHub Actions run and each `quality`, `integration`, `e2e` Job. On any failure, fetch the failing step log, reproduce it, add a failing regression, fix, push and repeat. Completion requires all three Jobs `success` and retained evidence artifacts.
