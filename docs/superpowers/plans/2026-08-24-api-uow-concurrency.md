# API Unit-of-Work Concurrency Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the globally shared `RunService` safe for concurrent Run submissions by constructing one Unit of Work and SQLAlchemy Session per `create_run()` call.

**Architecture:** `RunService` will depend on a zero-argument UoW factory rather than a mutable UoW instance. Production dependency assembly and every test call site will pass a factory; the existing transaction, idempotency, Outbox, API, and database contracts remain unchanged.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, PostgreSQL, pytest, Docker Compose, GitHub Actions.

## Global Constraints

- `POST /api/v1/runs`, its request fields, headers, 202 response, and error mapping must not change.
- Every `RunService.create_run()` call must receive a newly constructed Unit of Work and Session.
- Run, RunEvent, and OutboxEvent must remain one atomic PostgreSQL transaction.
- The unique idempotency-key constraint and conflict reread behavior must remain intact.
- Do not add locks, thread-local/context-variable state, database migrations, retries, or new dependencies.
- Do not support passing a prebuilt UoW instance to `RunService`; the constructor contract is factory-only.

---

## File Structure

- Modify `src/quality_flow/application/run_service.py`: define and consume the UoW factory contract.
- Modify `src/quality_flow/api/dependencies.py`: assemble `RunService` with a factory that returns a new SQLAlchemy UoW.
- Modify `tests/unit/test_run_service.py`: model shared persistence with distinct fake transaction objects and prove factory use.
- Modify `tests/unit/test_api_runs.py`: prove production dependency assembly supplies a fresh-UoW factory.
- Modify `tests/integration/test_run_persistence.py`: make the real PostgreSQL concurrency test share one service while using independent UoWs.
- Modify `tests/integration/test_worker_lifecycle.py`: migrate test-only RunService construction to the factory contract.
- Create `docs/superpowers/specs/2026-08-24-api-uow-concurrency-design.md`: retain the approved design and boundaries.
- Create `docs/superpowers/plans/2026-08-24-api-uow-concurrency.md`: retain this implementation and verification plan.

---

### Task 1: Make RunService Open a Fresh Transaction Per Call

**Files:**
- Modify: `src/quality_flow/application/run_service.py`
- Modify: `tests/unit/test_run_service.py`

**Interfaces:**
- Produces: `RunUnitOfWorkFactory = Callable[[], RunUnitOfWork]`.
- Produces: `RunService(uow_factory: RunUnitOfWorkFactory, registry: SuiteRegistry)`.
- Preserves: `RunService.create_run(suite_id: str, idempotency_key: str, parameters: dict[str, str]) -> Any`.

- [ ] **Step 1: Add a deterministic failing factory-lifecycle test**

Add one test that passes a callable returning two distinct fake UoWs to a single
shared `RunService`:

```python
def test_service_requests_a_fresh_uow_for_each_create(
    registry: SuiteRegistry,
) -> None:
    first_uow = FakeUnitOfWork()
    second_uow = FakeUnitOfWork()
    available = iter((first_uow, second_uow))

    service = RunService(lambda: next(available), registry)
    service.create_run("demo-api", "factory-first", {"scenario": "ok"})
    service.create_run("demo-api", "factory-second", {"scenario": "ok"})

    assert first_uow.commits == 1
    assert second_uow.commits == 1
    assert len(first_uow.created_runs) == 1
    assert len(second_uow.created_runs) == 1
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```powershell
python -m pytest tests/unit/test_run_service.py::test_service_requests_a_fresh_uow_for_each_create -q
```

Expected: FAIL because the current service tries to use the factory function
itself as a context manager.

- [ ] **Step 3: Implement the minimal factory contract**

In `run_service.py`, import `Callable`, define the alias after the UoW protocol,
store the factory, and call it inside `create_run()`:

```python
from collections.abc import Callable

RunUnitOfWorkFactory = Callable[[], RunUnitOfWork]


class RunService:
    def __init__(
        self,
        uow_factory: RunUnitOfWorkFactory,
        registry: SuiteRegistry,
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = registry

    def create_run(...):
        ...
        with self._uow_factory() as uow:
            ...
```

- [ ] **Step 4: Migrate fake persistence without weakening the contract**

Update the fake test support so each call creates a distinct `FakeUnitOfWork`,
while all instances share one backing `FakePersistence`:

```python
@dataclass
class FakePersistence:
    created_runs: list[FakeRun] = field(default_factory=list)
    run_events: list[Any] = field(default_factory=list)
    outbox_events: list[Any] = field(default_factory=list)
    commits: int = 0
    rollbacks: int = 0


class FakeUnitOfWork:
    def __init__(self, persistence: FakePersistence | None = None) -> None:
        self.persistence = persistence or FakePersistence()
        self.created_runs = self.persistence.created_runs
        self.run_events = self.persistence.run_events
        self.outbox_events = self.persistence.outbox_events
        self.runs = FakeRunRepository(self.created_runs)
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1
        self.persistence.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1
        self.persistence.rollbacks += 1
```

Use a factory that records every instance and shares `FakePersistence`. Assert
the duplicate-key test creates two distinct UoWs but persists one Run/Event/
Outbox and performs one commit. Assert invalid parameters invoke the factory zero
times, preserving validation-before-transaction behavior.

- [ ] **Step 5: Verify GREEN and regression behavior**

Run:

```powershell
python -m pytest tests/unit/test_run_service.py -q
```

Expected: all tests PASS, including fresh UoW, duplicate idempotency, snapshot,
and validation-before-transaction behavior.

- [ ] **Step 6: Commit Task 1**

```powershell
git add src/quality_flow/application/run_service.py tests/unit/test_run_service.py
git commit -m "fix: create a fresh uow per run request"
```

---

### Task 2: Wire Production and Prove Real Concurrency

**Files:**
- Modify: `src/quality_flow/api/dependencies.py`
- Modify: `tests/unit/test_api_runs.py`
- Modify: `tests/integration/test_run_persistence.py`
- Modify: `tests/integration/test_worker_lifecycle.py`

**Interfaces:**
- Consumes: `RunService(uow_factory: RunUnitOfWorkFactory, registry: SuiteRegistry)` from Task 1.
- Produces: application-scoped `RunService` backed by `partial(SqlAlchemyUnitOfWork, session_factory)`.
- Preserves: `ApiDependencies.run_service` and every route-visible behavior.

- [ ] **Step 1: Add a failing production-assembly test**

Monkeypatch `dependency_module.RunService` with a capturing constructor, call
`build_dependencies()`, then prove the first constructor argument is callable
and returns distinct UoWs:

```python
def test_dependency_builder_supplies_fresh_uow_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class CapturingRunService:
        def __init__(self, uow_factory: Any, _registry: Any) -> None:
            captured["uow_factory"] = uow_factory

    fake_session_factory = object()
    monkeypatch.setattr(dependency_module, "RunService", CapturingRunService)
    monkeypatch.setattr(dependency_module, "make_engine", lambda _url: object())
    monkeypatch.setattr(
        dependency_module,
        "make_session_factory",
        lambda _engine: fake_session_factory,
    )

    build_dependencies()

    factory = captured["uow_factory"]
    first = factory()
    second = factory()
    assert isinstance(first, SqlAlchemyUnitOfWork)
    assert isinstance(second, SqlAlchemyUnitOfWork)
    assert first is not second
```

- [ ] **Step 2: Run the assembly test and verify RED**

Run:

```powershell
python -m pytest tests/unit/test_api_runs.py::test_dependency_builder_supplies_fresh_uow_factory -q
```

Expected: FAIL because current production assembly passes a prebuilt UoW, which
is not callable.

- [ ] **Step 3: Wire the production factory**

Import `partial` and replace the prebuilt UoW with a zero-argument factory:

```python
from functools import partial

return ApiDependencies(
    run_service=RunService(
        partial(SqlAlchemyUnitOfWork, session_factory),
        registry,
    ),
    run_reader=SqlAlchemyRunReader(session_factory),
    readiness_check=check_readiness,
)
```

- [ ] **Step 4: Update all integration call sites and strengthen concurrency**

Replace each test-only prebuilt UoW with a factory, for example:

```python
RunService(lambda: SqlAlchemyUnitOfWork(session_factory), registry)
```

For constraint-failure coverage use:

```python
RunService(lambda: ConstraintFailingOutboxUnitOfWork(session_factory), registry)
```

Change the existing concurrent first-submission test so both threads share one
service and the factory creates one barrier UoW per call:

```python
barrier = Barrier(2)
created_uows: list[BarrierUnitOfWork] = []

def uow_factory() -> BarrierUnitOfWork:
    uow = BarrierUnitOfWork(session_factory, barrier)
    created_uows.append(uow)
    return uow

service = RunService(uow_factory, registry)

def submit() -> Run:
    return service.create_run(
        "demo-api", "concurrent-key", {"scenario": "ok"}
    )
```

Keep the existing database assertions and add:

```python
assert len(created_uows) == 2
assert created_uows[0] is not created_uows[1]
assert created_uows[0].session is not created_uows[1].session
```

- [ ] **Step 5: Verify focused unit and PostgreSQL integration behavior**

Run unit coverage:

```powershell
python -m pytest tests/unit/test_api_runs.py tests/unit/test_run_service.py -q
```

Run the focused integration test against the project's PostgreSQL service:

```powershell
docker compose -p quality-flow-uow-fix build api
docker compose -p quality-flow-uow-fix up -d --wait postgres redis
docker compose -p quality-flow-uow-fix run --rm --no-deps migrate
docker compose -p quality-flow-uow-fix run --rm --no-deps `
  -e DATABASE_URL=postgresql+psycopg://quality_flow:quality_flow@postgres:5432/quality_flow `
  api python -m pytest `
  tests/integration/test_run_persistence.py::test_concurrent_first_submission_returns_one_persisted_run `
  -q -p no:cacheprovider
```

Expected: all selected tests PASS; the concurrent test persists counts
`(1, 1, 1)` using two distinct UoWs and Sessions.

- [ ] **Step 6: Run the complete local validation matrix**

Run:

```powershell
python -m ruff check .
python -m pytest tests/unit -q
docker compose -p quality-flow-uow-fix run --rm --no-deps `
  -e DATABASE_URL=postgresql+psycopg://quality_flow:quality_flow@postgres:5432/quality_flow `
  -e TASK7_REDIS_URL=redis://redis:6379/14 `
  -e QUALITY_FLOW_TEST_ADMIN_DATABASE_URL=postgresql+psycopg://quality_flow:quality_flow@postgres:5432/postgres `
  api python -m pytest tests/integration -q -p no:cacheprovider
docker compose -p quality-flow-uow-fix down -v --remove-orphans
```

Run the complete black-box stack on a dedicated host port:

```powershell
$env:QUALITY_FLOW_API_PORT = "18081"
$env:QUALITY_FLOW_API_URL = "http://127.0.0.1:18081"
docker compose -p quality-flow-uow-e2e build api
docker compose -p quality-flow-uow-e2e up -d --wait --wait-timeout 180
python -m pytest tests/e2e -q
python scripts/ci_gate.py --api-url $env:QUALITY_FLOW_API_URL --suite-id demo-api --scenario ok --poll-interval 0.25 --timeout 90
docker compose -p quality-flow-uow-e2e down -v --remove-orphans
Remove-Item Env:QUALITY_FLOW_API_PORT, Env:QUALITY_FLOW_API_URL
```

Expected: Ruff, unit, integration, E2E, and the passing CI gate all succeed.
No command may be reported as passing without fresh output.

- [ ] **Step 7: Commit Task 2**

```powershell
git add src/quality_flow/api/dependencies.py tests/unit/test_api_runs.py tests/integration/test_run_persistence.py tests/integration/test_worker_lifecycle.py
git commit -m "test: cover concurrent run submission isolation"
```

After Task 2, scan active source and tests for stale prebuilt-UoW constructors:

```powershell
rg -n "RunService\(SqlAlchemyUnitOfWork|RunService\(ConstraintFailingOutboxUnitOfWork|RunService\(BarrierUnitOfWork" src tests
```

Expected: no matches. Generate a branch diff from its merge base, request
independent spec and code-quality review, fix every Critical/Important issue,
then repeat fresh verification before merge and push.
