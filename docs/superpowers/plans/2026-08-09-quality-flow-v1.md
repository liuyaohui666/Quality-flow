# QualityFlow V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a student-maintainable, Docker Compose runnable continuous test execution system that reliably executes registered pytest and Locust suites, persists structured results, and returns a CI quality-gate decision.

**Architecture:** A FastAPI control plane writes Run and Outbox records to PostgreSQL. A dispatcher publishes outbox events through Celery/Redis, an independent worker executes registered suites in per-attempt workspaces, and a reconciler marks expired leases as infrastructure failures. PostgreSQL is authoritative; Redis is transport only.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, Redis, Celery, pytest, Locust, Docker Compose, GitHub Actions.

## Global Constraints

- Only pre-registered trusted suites may execute; the API never accepts raw commands.
- PostgreSQL is the sole source of truth; Redis/Celery carry notifications only.
- Delivery is at least once; idempotency and database constraints prevent duplicate valid results.
- Run and RunAttempt remain separate entities.
- Test failures, infrastructure failures, and timeouts remain distinct.
- V1 has no automatic retry, cancellation, RBAC, UI console, Kubernetes, or untrusted-code sandbox.
- Every feature is developed test-first and receives an independently runnable verification command.
- No resume claim is written without an executable verification path.

---

## File Map

```text
quality-flow/
├── pyproject.toml
├── .env.example
├── .gitignore
├── Dockerfile
├── compose.yaml
├── alembic.ini
├── migrations/
├── config/suites.yaml
├── src/quality_flow/
│   ├── api/app.py
│   ├── api/dependencies.py
│   ├── api/routes/health.py
│   ├── api/routes/runs.py
│   ├── api/schemas.py
│   ├── application/run_service.py
│   ├── application/dispatcher.py
│   ├── application/reconciler.py
│   ├── domain/enums.py
│   ├── domain/state_machine.py
│   ├── infrastructure/artifacts.py
│   ├── infrastructure/celery_app.py
│   ├── infrastructure/config.py
│   ├── infrastructure/database.py
│   ├── infrastructure/logging.py
│   ├── infrastructure/models.py
│   ├── infrastructure/repositories.py
│   ├── runners/base.py
│   ├── runners/gates.py
│   ├── runners/locust_runner.py
│   ├── runners/parsers.py
│   ├── runners/pytest_runner.py
│   ├── suites/registry.py
│   └── worker/tasks.py
├── demo_target/app.py
├── demo_suites/api/test_target.py
├── demo_suites/load/locustfile.py
├── scripts/ci_gate.py
├── tests/unit/
├── tests/integration/
└── tests/e2e/
```

Each module has one responsibility: API translation, application orchestration, domain rules, infrastructure adapters, or test-runner adapters.

---

### Task 1: Project Foundation and Validated Suite Registry

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `src/quality_flow/__init__.py`
- Create: `src/quality_flow/infrastructure/config.py`
- Create: `src/quality_flow/suites/registry.py`
- Create: `config/suites.yaml`
- Test: `tests/unit/test_suite_registry.py`

**Interfaces:**
- Produces: `Settings`, `SuiteRegistry`, `SuiteDefinition`, `GatePolicy`, `registry.get(suite_id)`.
- The Run service and runners consume immutable `SuiteDefinition` values.

- [ ] **Step 1: Write failing registry tests**

```python
def test_registry_rejects_unknown_suite(registry):
    with pytest.raises(UnknownSuiteError):
        registry.get("arbitrary-command")

def test_registry_rejects_parameter_outside_allowlist(registry):
    suite = registry.get("demo-api")
    with pytest.raises(InvalidSuiteParameter):
        suite.resolve_parameters({"scenario": "; rm -rf /"})
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/unit/test_suite_registry.py -q`

Expected: import failure because `SuiteRegistry` does not exist.

- [ ] **Step 3: Implement typed settings and registry**

Implement immutable dataclasses with these signatures:

```python
@dataclass(frozen=True)
class GatePolicy:
    min_pass_rate: float = 1.0
    max_failures: int = 0
    max_error_rate: float | None = None
    max_p95_ms: float | None = None
    min_requests: int | None = None

@dataclass(frozen=True)
class SuiteDefinition:
    suite_id: str
    runner_type: Literal["pytest", "locust"]
    working_directory: Path
    argv: tuple[str, ...]
    timeout_seconds: int
    allowed_parameters: Mapping[str, tuple[str, ...]]
    gate_policy: GatePolicy
    source_revision: str
```

Required methods and return types:

- `SuiteDefinition.resolve_parameters(supplied: Mapping[str, str]) -> dict[str, str]`
- `SuiteRegistry.from_yaml(path: Path, project_root: Path) -> SuiteRegistry`
- `SuiteRegistry.get(suite_id: str) -> SuiteDefinition`

Reject absolute working directories outside project root, `..` traversal, raw shell strings, unknown parameters, and values outside explicit allowlists.

- [ ] **Step 4: Run registry tests**

Run: `python -m pytest tests/unit/test_suite_registry.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml .gitignore .env.example config src tests/unit/test_suite_registry.py
git commit -m "feat: add validated suite registry"
```

---

### Task 2: Domain Status Model and Quality-Gate Rules

**Files:**
- Create: `src/quality_flow/domain/enums.py`
- Create: `src/quality_flow/domain/state_machine.py`
- Create: `src/quality_flow/runners/base.py`
- Create: `src/quality_flow/runners/gates.py`
- Test: `tests/unit/test_state_machine.py`
- Test: `tests/unit/test_gates.py`

**Interfaces:**
- Produces: `RunStatus`, `RunOutcome`, `AttemptStatus`, `ensure_run_transition()`, `evaluate_functional_gate()`, `evaluate_performance_gate()`.
- Repository and worker code must call the state-machine service instead of assigning arbitrary status values.

- [ ] **Step 1: Write failing state and gate tests**

```python
def test_completed_run_cannot_return_to_running():
    with pytest.raises(InvalidStateTransition):
        ensure_run_transition(RunStatus.COMPLETED, RunStatus.RUNNING)

def test_functional_gate_fails_below_required_pass_rate():
    result = evaluate_functional_gate(
        CaseSummary(total=10, passed=9, failed=1, errors=0, skipped=0),
        GatePolicy(min_pass_rate=1.0, max_failures=0),
    )
    assert result.passed is False
    assert "pass_rate" in result.reason_codes
```

- [ ] **Step 2: Verify failures**

Run: `python -m pytest tests/unit/test_state_machine.py tests/unit/test_gates.py -q`

Expected: imports fail because domain types are missing.

- [ ] **Step 3: Implement domain enums and pure gate functions**

Required run transitions:

```text
QUEUED -> RUNNING
RUNNING -> COMPLETED | INFRA_FAILED | TIMED_OUT
```

Required attempt transitions:

```text
DISPATCHED -> RUNNING
RUNNING -> PASSED | TEST_FAILED | INFRA_FAILED | TIMED_OUT | ABANDONED
```

Gate functions return an immutable `GateResult(passed: bool, reason_codes: tuple[str, ...], details: dict[str, float])` and never access the database.

- [ ] **Step 4: Run domain tests**

Run: `python -m pytest tests/unit/test_state_machine.py tests/unit/test_gates.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/quality_flow/domain src/quality_flow/runners/base.py src/quality_flow/runners/gates.py tests/unit
git commit -m "feat: define execution states and quality gates"
```

---

### Task 3: PostgreSQL Schema, Migrations, and Transactional Run Creation

**Files:**
- Create: `src/quality_flow/infrastructure/database.py`
- Create: `src/quality_flow/infrastructure/models.py`
- Create: `src/quality_flow/infrastructure/repositories.py`
- Create: `src/quality_flow/application/run_service.py`
- Create: `alembic.ini`
- Create: `compose.yaml` with PostgreSQL and Redis development services
- Create: `migrations/env.py`
- Create: `migrations/versions/0001_initial_schema.py`
- Test: `tests/unit/test_run_service.py`
- Test: `tests/integration/test_run_persistence.py`

**Interfaces:**
- Produces: `RunService.create_run()`, `RunRepository.claim_queued_run()`, `RunRepository.record_terminal_result()`.
- API consumes `RunService`; worker consumes repository claim and terminal methods.

- [ ] **Step 1: Write failing service tests with a fake unit of work**

```python
def test_duplicate_idempotency_key_returns_existing_run(fake_uow, registry):
    service = RunService(fake_uow, registry)
    first = service.create_run("demo-api", "same-key", {"scenario": "ok"})
    second = service.create_run("demo-api", "same-key", {"scenario": "ok"})
    assert first.run_id == second.run_id
    assert len(fake_uow.outbox_events) == 1
```

- [ ] **Step 2: Verify service test fails**

Run: `python -m pytest tests/unit/test_run_service.py -q`

Expected: `RunService` is missing.

- [ ] **Step 3: Implement ORM models and service transaction**

Create tables: `runs`, `run_attempts`, `case_results`, `metrics`, `artifacts`, `gate_evaluations`, `run_events`, and `outbox_events`.

Required constraints:

- unique `runs.idempotency_key`;
- unique `(run_id, attempt_no)`;
- unique `(attempt_id, node_id)` for case results;
- unique `(attempt_id, metric_name)` for metrics;
- foreign keys with explicit delete behavior;
- UTC timestamps and indexed status/created-at columns.

`create_run()` must insert the Run, initial RunEvent, and OutboxEvent in one transaction. Store the resolved suite and gate policy snapshot as JSON.

- [ ] **Step 4: Run migration against PostgreSQL and integration tests**

Run: `docker compose up -d postgres`

Run: `python -m alembic upgrade head`

Run: `python -m pytest tests/unit/test_run_service.py tests/integration/test_run_persistence.py -q`

Expected: migration succeeds; duplicate keys return one Run; a forced transaction failure leaves neither Run nor Outbox row.

- [ ] **Step 5: Commit**

```powershell
git add src/quality_flow/infrastructure src/quality_flow/application/run_service.py migrations alembic.ini tests
git commit -m "feat: persist runs with transactional outbox"
```

---

### Task 4: FastAPI Control Plane and CI Client Contract

**Files:**
- Create: `src/quality_flow/api/app.py`
- Create: `src/quality_flow/api/dependencies.py`
- Create: `src/quality_flow/api/schemas.py`
- Create: `src/quality_flow/api/routes/health.py`
- Create: `src/quality_flow/api/routes/runs.py`
- Create: `scripts/ci_gate.py`
- Test: `tests/unit/test_api_runs.py`
- Test: `tests/unit/test_ci_gate.py`

**Interfaces:**
- Produces: `POST /api/v1/runs`, `GET /api/v1/runs/{run_id}`, `GET /api/v1/runs/{run_id}/events`, `GET /api/v1/runs/{run_id}/artifacts`, health endpoints, and CI polling exit semantics.

- [ ] **Step 1: Write failing API contract tests**

```python
def test_submit_registered_suite_returns_202(client):
    response = client.post(
        "/api/v1/runs",
        headers={"Idempotency-Key": "ci-123"},
        json={"suite_id": "demo-api", "parameters": {"scenario": "ok"}},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "QUEUED"

def test_submit_rejects_unknown_suite(client):
    response = client.post(
        "/api/v1/runs",
        headers={"Idempotency-Key": "ci-unknown"},
        json={"suite_id": "shell", "parameters": {}},
    )
    assert response.status_code == 404
```

- [ ] **Step 2: Verify API tests fail**

Run: `python -m pytest tests/unit/test_api_runs.py -q`

Expected: application import fails.

- [ ] **Step 3: Implement routes and schemas**

The POST route requires a non-empty `Idempotency-Key`, rejects raw command fields, and returns the existing Run for duplicate keys. Query responses expose status, outcome, timestamps, attempt summary, gate result, case summary, metrics, and artifact metadata without local paths.

`scripts/ci_gate.py` accepts API URL, suite ID, scenario, poll interval, and total timeout. It returns 0 only for `COMPLETED/PASSED`; all other terminal outcomes or polling timeout return non-zero.

- [ ] **Step 4: Run API and CI client tests**

Run: `python -m pytest tests/unit/test_api_runs.py tests/unit/test_ci_gate.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/quality_flow/api scripts tests/unit
git commit -m "feat: expose run control API and CI client"
```

---

### Task 5: Artifact Store, Redaction, and Result Parsers

**Files:**
- Create: `src/quality_flow/infrastructure/artifacts.py`
- Create: `src/quality_flow/infrastructure/logging.py`
- Create: `src/quality_flow/runners/parsers.py`
- Test: `tests/unit/test_artifacts.py`
- Test: `tests/unit/test_redaction.py`
- Test: `tests/unit/test_result_parsers.py`

**Interfaces:**
- Produces: `FileArtifactStore.put()`, `FileArtifactStore.resolve()`, `parse_junit_xml()`, `parse_locust_stats()`, and structured redaction helpers.
- Runners consume parsers and ArtifactStore.

- [ ] **Step 1: Write failing security and parser tests**

```python
def test_artifact_store_rejects_path_outside_attempt(tmp_path):
    store = FileArtifactStore(tmp_path / "artifacts")
    with pytest.raises(UnsafeArtifactPath):
        store.put(tmp_path / "outside.log", metadata())

def test_redactor_masks_nested_authorization_and_cookie():
    value = {"headers": {"Authorization": "Bearer secret", "Cookie": "token=x"}}
    assert redact(value)["headers"] == {"Authorization": "***", "Cookie": "***"}
```

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest tests/unit/test_artifacts.py tests/unit/test_redaction.py tests/unit/test_result_parsers.py -q`

Expected: missing-module failures.

- [ ] **Step 3: Implement safe artifact persistence and parsers**

Artifact writes use a temporary file followed by `Path.replace()`, calculate SHA-256 and size, enforce configured limits, and return an opaque relative URI. JUnit parsing returns `CaseResultData` and `CaseSummary`. Locust parsing reads the aggregate row from CSV and returns request count, failure ratio, requests per second, average, and P95.

- [ ] **Step 4: Run unit tests**

Run: `python -m pytest tests/unit/test_artifacts.py tests/unit/test_redaction.py tests/unit/test_result_parsers.py -q`

Expected: all tests pass, including malformed XML/CSV cases.

- [ ] **Step 5: Commit**

```powershell
git add src/quality_flow/infrastructure/artifacts.py src/quality_flow/infrastructure/logging.py src/quality_flow/runners/parsers.py tests/unit
git commit -m "feat: secure artifacts and parse runner results"
```

---

### Task 6: Pytest and Locust Runner Adapters

**Files:**
- Create: `src/quality_flow/runners/pytest_runner.py`
- Create: `src/quality_flow/runners/locust_runner.py`
- Create: `demo_suites/api/test_target.py`
- Create: `demo_suites/load/locustfile.py`
- Test: `tests/unit/test_pytest_runner.py`
- Test: `tests/unit/test_locust_runner.py`

**Interfaces:**
- Produces: `PytestRunner.run(spec, workspace, heartbeat)`, `LocustRunner.run(spec, workspace, heartbeat)` returning `RunnerOutcome`.
- Worker chooses the runner by the immutable suite snapshot.

- [ ] **Step 1: Write failing runner tests using temporary fixture projects**

```python
def test_pytest_runner_classifies_assertion_failure_as_test_failure(tmp_path):
    outcome = runner_with_failing_test(tmp_path).run(spec(), tmp_path, noop_heartbeat)
    assert outcome.attempt_status is AttemptStatus.TEST_FAILED
    assert outcome.case_summary.failed == 1

def test_pytest_runner_kills_process_group_on_timeout(tmp_path):
    outcome = runner_with_sleeping_test(tmp_path).run(short_timeout_spec(), tmp_path, noop_heartbeat)
    assert outcome.attempt_status is AttemptStatus.TIMED_OUT
```

- [ ] **Step 2: Verify runner tests fail**

Run: `python -m pytest tests/unit/test_pytest_runner.py tests/unit/test_locust_runner.py -q`

Expected: runner imports fail.

- [ ] **Step 3: Implement safe subprocess execution**

Use `subprocess.Popen(argv, shell=False, cwd=validated_workspace, env=allowlisted_env, stdout=PIPE, stderr=PIPE, start_new_session=True)`. Poll with a short interval to call the heartbeat. Enforce timeout, output-size cap, and process-group termination. Pytest forces JUnit output; Locust forces `--headless --csv` output. Missing or malformed required result files produce `INFRA_FAILED`.

- [ ] **Step 4: Run runner tests**

Run: `python -m pytest tests/unit/test_pytest_runner.py tests/unit/test_locust_runner.py -q`

Expected: passing, assertion-failure, internal-error, timeout, normal-load, and degraded-load fixtures are classified correctly.

- [ ] **Step 5: Commit**

```powershell
git add src/quality_flow/runners demo_suites tests/unit
git commit -m "feat: execute pytest and locust suites safely"
```

---

### Task 7: Outbox Dispatcher, Celery Worker, Lease, and Reconciler

**Files:**
- Create: `src/quality_flow/infrastructure/celery_app.py`
- Create: `src/quality_flow/application/dispatcher.py`
- Create: `src/quality_flow/application/reconciler.py`
- Create: `src/quality_flow/worker/tasks.py`
- Test: `tests/unit/test_dispatcher.py`
- Test: `tests/integration/test_worker_lifecycle.py`

**Interfaces:**
- Produces: `dispatch_once()`, Celery task `execute_run(run_id)`, `reconcile_once(now)`, worker heartbeat and terminal-result transaction.

- [ ] **Step 1: Write failing dispatcher and lifecycle tests**

```python
def test_dispatch_failure_keeps_outbox_pending(dispatcher, broker_that_fails):
    dispatcher.dispatch_once()
    assert dispatcher.pending_events()[0].status == "PENDING"

def test_reconciler_marks_expired_attempt_abandoned(repository, expired_attempt):
    reconcile_once(repository, now=expired_attempt.lease_expires_at + timedelta(seconds=1))
    assert repository.get_attempt(expired_attempt.id).status is AttemptStatus.ABANDONED
    assert repository.get_run(expired_attempt.run_id).status is RunStatus.INFRA_FAILED
```

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest tests/unit/test_dispatcher.py tests/integration/test_worker_lifecycle.py -q`

Expected: lifecycle modules are missing.

- [ ] **Step 3: Implement delivery and worker lifecycle**

Dispatcher publishes only event identifiers and run IDs, marks sent only after broker acknowledgement, and retries pending events on later polling cycles. Worker claims with a conditional database update, creates Attempt 1, updates heartbeat/lease during execution, persists results/artifacts/gate in one terminal transaction, then returns to Celery. A duplicate task for a non-queued Run is a no-op. Reconciler marks stale attempts and appends RunEvent records; it does not retry in V1.

- [ ] **Step 4: Run lifecycle tests with PostgreSQL and Redis**

Run: `docker compose up -d postgres redis`

Run: `python -m pytest tests/unit/test_dispatcher.py tests/integration/test_worker_lifecycle.py -q`

Expected: outbox recovery, duplicate delivery, heartbeat, terminal write, and stale lease tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/quality_flow/application src/quality_flow/infrastructure/celery_app.py src/quality_flow/worker tests
git commit -m "feat: dispatch and execute runs with lease tracking"
```

---

### Task 8: Controlled Target and Full Docker Compose E2E

**Files:**
- Create: `demo_target/app.py`
- Create: `Dockerfile`
- Modify: `compose.yaml` to add application and target services
- Create: `scripts/wait_for_run.py`
- Test: `tests/e2e/test_quality_flow.py`

**Interfaces:**
- Produces: deterministic `/health`, `/work?mode=ok`, `/work?mode=error`, and `/work?mode=slow` endpoints plus the complete local stack.

- [ ] **Step 1: Write the E2E expectations**

```python
@pytest.mark.parametrize(
    ("suite_id", "scenario", "status", "outcome"),
    [
        ("demo-api", "ok", "COMPLETED", "PASSED"),
        ("demo-api", "error", "COMPLETED", "FAILED"),
        ("demo-api", "slow", "TIMED_OUT", "UNKNOWN"),
        ("demo-load", "baseline", "COMPLETED", "PASSED"),
        ("demo-load", "degraded", "COMPLETED", "FAILED"),
    ],
)
def test_registered_demo_scenarios(api_client, suite_id, scenario, status, outcome):
    key = f"e2e-{suite_id}-{scenario}-{uuid4()}"
    submit = api_client.post(
        "/api/v1/runs",
        headers={"Idempotency-Key": key},
        json={"suite_id": suite_id, "parameters": {"scenario": scenario}},
    )
    assert submit.status_code == 202

    run_id = submit.json()["run_id"]
    result = wait_for_terminal_run(api_client, run_id, timeout_seconds=90)

    assert result["status"] == status
    assert result["outcome"] == outcome
```

- [ ] **Step 2: Verify E2E cannot run yet**

Run: `python -m pytest tests/e2e/test_quality_flow.py -q`

Expected: connection failure because the stack does not exist.

- [ ] **Step 3: Implement target and Compose services**

Compose services: `postgres`, `redis`, `migrate`, `api`, `dispatcher`, `worker`, `reconciler`, and `demo-target`. Use health checks and dependency conditions. API, dispatcher, worker, and reconciler use the same image with different commands. A named volume stores artifacts. No host Docker socket is mounted.

- [ ] **Step 4: Run the full E2E suite**

Run: `docker compose up -d --build`

Run: `python -m pytest tests/e2e/test_quality_flow.py -q`

Expected: all five scenarios and duplicate idempotency checks pass.

- [ ] **Step 5: Commit**

```powershell
git add demo_target Dockerfile compose.yaml scripts tests/e2e
git commit -m "feat: deliver reproducible end-to-end stack"
```

---

### Task 9: CI, Documentation, and Evidence Matrix

**Files:**
- Create: `.github/workflows/quality-flow.yml`
- Create: `README.md`
- Create: `docs/architecture.md`
- Create: `docs/evidence-matrix.md`
- Modify: `docs/superpowers/specs/2026-08-09-quality-flow-design.md`
- Test: verification commands below.

**Interfaces:**
- Produces: reproducible onboarding, CI validation, architecture explanation, and a direct mapping from resume statements to evidence.

- [ ] **Step 1: Add CI that runs fast tests and Compose E2E**

Workflow steps:

1. checkout;
2. set up Python 3.12;
3. install `.[dev]`;
4. run Ruff and unit tests;
5. build and start Compose;
6. run migrations and E2E tests;
7. always upload logs and artifacts;
8. always print Compose logs on failure.

- [ ] **Step 2: Write README and architecture documentation**

README must include project positioning, trust boundary, components, local setup, API examples, CI-gate command, expected successful and failing outcomes, failure-diagnosis path, test commands, known limitations, and honest evolution path.

- [ ] **Step 3: Write evidence matrix**

Required columns:

```text
Claim | Implementation | Verification command | Evidence artifact | Limitation
```

Include idempotency, outbox recovery, timeout classification, Worker lease expiry, pytest gate, Locust gate, artifact isolation, and CI exit status.

- [ ] **Step 4: Run complete verification**

Run: `python -m ruff check .`

Run: `python -m pytest tests/unit -q`

Run: `python -m pytest tests/integration -q`

Run: `python -m pytest tests/e2e -q`

Run: `docker compose config`

Expected: all commands exit 0.

- [ ] **Step 5: Commit**

```powershell
git add .github README.md docs
git commit -m "docs: complete QualityFlow v1 delivery"
```

---

## Final Review Gate

Before claiming completion:

1. start from an empty database and artifact volume;
2. build every image without local Python dependencies;
3. execute all five E2E scenarios;
4. inspect one passing and one failing Run through the API;
5. confirm duplicate submission returns the original Run;
6. stop a Worker during a run and confirm lease reconciliation;
7. inspect artifact hashes and isolated paths;
8. confirm secrets are absent from Git and captured logs;
9. confirm `git status --short` contains no unintended files;
10. record only measured outcomes in README and resume material.
