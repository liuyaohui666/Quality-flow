# Controlled Worker Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one PostgreSQL-authoritative automatic retry when an enabled suite loses its Worker lease, while preserving Attempt history and never retrying ordinary test failures.

**Architecture:** Retry policy is validated in `SuiteRegistry` and frozen into each Run snapshot. `LeaseReconciler` atomically abandons Attempt 1, returns the Run to `queued`, writes `run.retry_scheduled`, and inserts a fresh Outbox row; the existing Dispatcher/Celery/Redis/Worker path creates Attempt 2. Existing Run locks, lease tokens, and the `(run_id, attempt_no)` unique constraint prevent duplicate effective Attempts.

**Tech Stack:** Python 3.12, pytest, FastAPI/Pydantic, SQLAlchemy 2, PostgreSQL, Celery/Redis, Docker Compose, GitHub Actions.

## Global Constraints

- The same Run has at most two Attempts: the initial Attempt plus one retry.
- Only `worker_lost` caused by an expired lease is retryable in V1.1.
- Test, gate, timeout, configuration, parsing, output, and artifact failures are not retried.
- Missing retry policy in old Run snapshots means retries are disabled.
- Retry scheduling and the new Outbox event must be one PostgreSQL transaction.
- Restful Booker remains retry-disabled because it writes to a shared external service.
- Do not add delayed backoff, a `retrying` state, Celery `self.retry()`, or a third Attempt.

---

## File Structure

- Modify `src/quality_flow/suites/registry.py`: own immutable `RetryPolicy` parsing and validation.
- Modify `config/suites.yaml`: enable one retry only for `demo-api`.
- Modify `src/quality_flow/application/run_service.py`: freeze policy into `suite_snapshot`.
- Modify `src/quality_flow/domain/state_machine.py`: permit the controlled `running -> queued` Run transition.
- Modify `src/quality_flow/application/reconciler.py`: own expired-lease retry decision and atomic Outbox creation.
- Modify `src/quality_flow/infrastructure/repositories.py`: preserve the first Run start time when Attempt 2 is claimed.
- Modify `src/quality_flow/application/worker.py`: terminate artifact persistence errors without retry.
- Modify `src/quality_flow/api/dependencies.py`: load cases, metrics, and gates only for the latest Attempt.
- Modify `README.md`: document policy, lifecycle, scope, and interview explanation.
- Modify existing unit/integration tests; no new production module or migration is needed.

---

### Task 1: Validate and Snapshot Retry Policy

**Files:**
- Modify: `src/quality_flow/suites/registry.py`
- Modify: `config/suites.yaml`
- Modify: `src/quality_flow/application/run_service.py`
- Test: `tests/unit/test_suite_registry.py`
- Test: `tests/unit/test_run_service.py`

**Interfaces:**
- Produces: `RetryPolicy(max_attempts: int, retry_on: frozenset[str])`.
- Produces: `SuiteDefinition.retry_policy: RetryPolicy`.
- Produces snapshot key: `suite_snapshot["retry_policy"] == {"max_attempts": int, "retry_on": list[str]}`.

- [ ] **Step 1: Write failing registry tests**

Add tests that assert `demo-api` resolves to two maximum Attempts with
`worker_lost`, Restful Booker defaults to one Attempt, the policy is immutable,
and invalid values are rejected:

```python
def test_registry_parses_conservative_retry_policy(registry: SuiteRegistry) -> None:
    policy = registry.get("demo-api").retry_policy
    assert policy.max_attempts == 2
    assert policy.retry_on == frozenset({"worker_lost"})
    assert registry.get("restful-booker-api").retry_policy.max_attempts == 1


@pytest.mark.parametrize(
    "policy_yaml",
    [
        "max_attempts: 0\n    retry_on: [worker_lost]",
        "max_attempts: true\n    retry_on: [worker_lost]",
        "max_attempts: 3\n    retry_on: [worker_lost]",
        "max_attempts: 2\n    retry_on: [timeout]",
    ],
)
def test_registry_rejects_invalid_retry_policy(
    tmp_path: Path, policy_yaml: str
) -> None:
    config = tmp_path / "suites.yaml"
    config.write_text(
        "suites:\n  demo:\n    runner_type: pytest\n"
        "    working_directory: .\n    argv: [python]\n"
        "    timeout_seconds: 1\n    source_revision: test\n"
        f"    retry_policy:\n      {policy_yaml.replace(chr(10), chr(10) + '      ')}\n",
        encoding="utf-8",
    )
    with pytest.raises(SuiteRegistryError, match="retry_policy"):
        SuiteRegistry.from_yaml(config, tmp_path)
```

- [ ] **Step 2: Run the registry tests and verify RED**

Run: `python -m pytest tests/unit/test_suite_registry.py -q`

Expected: failures because `SuiteDefinition` has no `retry_policy`.

- [ ] **Step 3: Implement immutable parsing**

Add the production type and strict parser:

```python
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    retry_on: frozenset[str] = frozenset()


@staticmethod
def _parse_retry_policy(raw_policy: object, suite_id: str) -> RetryPolicy:
    if raw_policy is None:
        return RetryPolicy()
    if not isinstance(raw_policy, dict) or set(raw_policy) - {"max_attempts", "retry_on"}:
        raise SuiteRegistryError(f"Suite {suite_id!r} has an invalid retry_policy")
    max_attempts = raw_policy.get("max_attempts", 1)
    retry_on = raw_policy.get("retry_on", [])
    if type(max_attempts) is not int or max_attempts not in (1, 2):
        raise SuiteRegistryError(f"Suite {suite_id!r} retry_policy max_attempts must be 1 or 2")
    if isinstance(retry_on, str) or not isinstance(retry_on, list):
        raise SuiteRegistryError(f"Suite {suite_id!r} retry_policy retry_on must be a list")
    if not all(reason == "worker_lost" for reason in retry_on):
        raise SuiteRegistryError(f"Suite {suite_id!r} retry_policy contains an unknown reason")
    reasons = frozenset(retry_on)
    if reasons and max_attempts != 2:
        raise SuiteRegistryError(f"Suite {suite_id!r} retry_policy needs max_attempts 2")
    return RetryPolicy(max_attempts=max_attempts, retry_on=reasons)
```

Add `retry_policy` to `SuiteDefinition`, invoke the parser from `_parse_suite`,
and add this only to `demo-api` in YAML:

```yaml
retry_policy:
  max_attempts: 2
  retry_on:
    - worker_lost
```

- [ ] **Step 4: Write and run the snapshot RED test**

Extend `test_create_run_stores_resolved_suite_and_gate_policy_snapshots`:

```python
assert run.suite_snapshot["retry_policy"] == {
    "max_attempts": 2,
    "retry_on": ["worker_lost"],
}
```

Run: `python -m pytest tests/unit/test_run_service.py -q`

Expected: FAIL because the Run snapshot lacks `retry_policy`.

- [ ] **Step 5: Freeze the normalized policy into each Run**

Add to the existing `suite_snapshot` mapping:

```python
"retry_policy": {
    "max_attempts": suite.retry_policy.max_attempts,
    "retry_on": sorted(suite.retry_policy.retry_on),
},
```

Run: `python -m pytest tests/unit/test_suite_registry.py tests/unit/test_run_service.py -q`

Expected: all selected tests PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add config/suites.yaml src/quality_flow/suites/registry.py src/quality_flow/application/run_service.py tests/unit/test_suite_registry.py tests/unit/test_run_service.py
git commit -m "feat: define controlled retry policy"
```

---

### Task 2: Atomically Schedule One Retry

**Files:**
- Modify: `src/quality_flow/domain/state_machine.py`
- Modify: `src/quality_flow/application/reconciler.py`
- Modify: `src/quality_flow/infrastructure/repositories.py`
- Test: `tests/unit/test_state_machine.py`
- Test: `tests/integration/test_worker_lifecycle.py`

**Interfaces:**
- Consumes: `suite_snapshot["retry_policy"]` from Task 1.
- Produces: `run.retry_scheduled` RunEvent and a new unpublished `OutboxEvent`.
- Produces: Run `running -> queued` only for an eligible expired first Attempt.

- [ ] **Step 1: Write state-machine RED test**

```python
def test_running_run_can_return_to_queue_for_controlled_retry() -> None:
    ensure_run_transition(RunStatus.RUNNING, RunStatus.QUEUED)
```

Run: `python -m pytest tests/unit/test_state_machine.py::test_running_run_can_return_to_queue_for_controlled_retry -q`

Expected: FAIL with `InvalidStateTransition`.

- [ ] **Step 2: Add the single retry transition**

Add `RunStatus.QUEUED` to the allowed successors of `RunStatus.RUNNING`, then rerun
the state-machine file.

Run: `python -m pytest tests/unit/test_state_machine.py -q`

Expected: PASS.

- [ ] **Step 3: Write real-PostgreSQL retry lifecycle tests**

Build on the existing lifecycle fixtures and assert the first expiry atomically
requeues while the second expiry terminates:

```python
reconcile_at = attempt.lease_expires_at
assert LeaseReconciler(session_factory).reconcile_once(now=reconcile_at) == 1

with session_factory() as session:
    run = session.get(Run, run_id)
    attempts = session.scalars(
        select(RunAttempt).where(RunAttempt.run_id == run_id).order_by(RunAttempt.attempt_no)
    ).all()
    retries = session.scalars(
        select(RunEvent).where(
            RunEvent.run_id == run_id,
            RunEvent.event_type == "run.retry_scheduled",
        )
    ).all()
    retry_outbox = session.scalars(
        select(OutboxEvent).where(
            OutboxEvent.aggregate_id == run_id,
            OutboxEvent.event_type == "run.retry_scheduled",
        )
    ).all()
    assert run.status is RunStatus.QUEUED
    assert run.outcome is RunOutcome.UNKNOWN
    assert run.finished_at is None
    assert attempts[0].status is AttemptStatus.ABANDONED
    assert len(retries) == len(retry_outbox) == 1
```

Also assert that claiming the requeued Run creates Attempt 2, preserves Run
`started_at`, and a second expiry produces `infra_failed/unknown` without a third
Outbox row.

- [ ] **Step 4: Run the lifecycle tests and verify RED**

Run:

```powershell
$env:DATABASE_URL='postgresql+psycopg://quality_flow@127.0.0.1:55432/quality_flow'
python -m pytest tests/integration/test_worker_lifecycle.py -q -k 'retry or expired'
```

Expected: new retry tests FAIL because the Reconciler terminates the Run.

- [ ] **Step 5: Implement retry decision and atomic write**

Add a safe snapshot helper:

```python
def _allows_worker_lost_retry(run: Run, attempt: RunAttempt) -> bool:
    raw = run.suite_snapshot.get("retry_policy")
    if not isinstance(raw, dict):
        return False
    max_attempts = raw.get("max_attempts")
    retry_on = raw.get("retry_on")
    return (
        type(max_attempts) is int
        and max_attempts == 2
        and isinstance(retry_on, list)
        and "worker_lost" in retry_on
        and attempt.attempt_no < max_attempts
    )
```

Inside the existing locked/rechecked transaction, terminate the Attempt and
branch:

```python
attempt.status = AttemptStatus.ABANDONED
attempt.finished_at = reconciled_at
attempt.failure_reason = "worker lease expired"

if _allows_worker_lost_retry(run, attempt):
    ensure_run_transition(run.status, RunStatus.QUEUED)
    run.status = RunStatus.QUEUED
    run.outcome = RunOutcome.UNKNOWN
    run.finished_at = None
    run.updated_at = reconciled_at
    session.add(RunEvent(
        run_id=run_id,
        event_type="run.retry_scheduled",
        payload={
            "status": RunStatus.QUEUED.value,
            "outcome": RunOutcome.UNKNOWN.value,
            "attempt_no": attempt.attempt_no,
            "next_attempt_no": attempt.attempt_no + 1,
            "reason": "worker_lost",
        },
        created_at=reconciled_at,
    ))
    session.add(OutboxEvent(
        aggregate_type="run",
        aggregate_id=run_id,
        event_type="run.retry_scheduled",
        payload={"run_id": str(run_id)},
        created_at=reconciled_at,
    ))
else:
    ensure_run_transition(run.status, RunStatus.INFRA_FAILED)
    run.status = RunStatus.INFRA_FAILED
    run.outcome = RunOutcome.UNKNOWN
    run.finished_at = reconciled_at
    run.updated_at = reconciled_at
    session.add(RunEvent(
        run_id=run_id,
        event_type="run.abandoned",
        payload={
            "status": RunStatus.INFRA_FAILED.value,
            "outcome": RunOutcome.UNKNOWN.value,
            "attempt_status": AttemptStatus.ABANDONED.value,
            "attempt_id": str(attempt_id),
        },
        created_at=reconciled_at,
    ))
```

Change claim assignment from `run.started_at = now` to:

```python
run.started_at = run.started_at or now
```

- [ ] **Step 6: Add concurrency and rollback coverage**

Use the existing barrier/session wrappers to run two Reconcilers and assert one
retry event/Outbox. Inject a commit failure after adding the Outbox and assert the
Run and Attempt remain `running` with no retry event.

Run: same real-PostgreSQL command as Step 4.

Expected: all selected lifecycle tests PASS.

- [ ] **Step 7: Commit Task 2**

```bash
git add src/quality_flow/domain/state_machine.py src/quality_flow/application/reconciler.py src/quality_flow/infrastructure/repositories.py tests/unit/test_state_machine.py tests/integration/test_worker_lifecycle.py
git commit -m "feat: retry expired worker lease once"
```

---

### Task 3: Preserve Failure Boundaries and Latest-Attempt Results

**Files:**
- Modify: `src/quality_flow/application/worker.py`
- Modify: `src/quality_flow/api/dependencies.py`
- Test: `tests/integration/test_worker_lifecycle.py`
- Test: `tests/unit/test_api_runs.py`

**Interfaces:**
- Consumes: existing `FileArtifactStore.put()` and `record_terminal_aggregate()`.
- Produces: non-retryable artifact failure terminal state.
- Produces: API top-level cases/metrics/gates for only the latest Attempt.

- [ ] **Step 1: Write artifact-failure RED test**

Inject a store whose `put()` raises `OSError("sentinel path")`, execute a claimed
Run, and assert:

```python
assert run.status is RunStatus.INFRA_FAILED
assert run.outcome is RunOutcome.UNKNOWN
assert run.attempts[0].status is AttemptStatus.INFRA_FAILED
assert "sentinel path" not in (run.attempts[0].failure_reason or "")
assert retry_event_count == 0
assert retry_outbox_count == 0
```

Run the named integration test and verify it fails because the Attempt remains
`running`.

- [ ] **Step 2: Record a safe non-retryable artifact failure**

Wrap artifact persistence only, and add a focused helper:

```python
def _record_artifact_failure(self, lease: ClaimedLease) -> None:
    failed_at = self._clock()
    outcome = RunnerOutcome(
        attempt_status=AttemptStatus.INFRA_FAILED,
        exit_code=None,
        started_at=lease.started_at,
        finished_at=failed_at,
        gate_result=None,
        failure_kind="artifact_store_failed",
        failure_summary="artifact persistence failed",
    )
    with SqlAlchemyUnitOfWork(self._session_factory) as uow:
        uow.runs.record_terminal_aggregate(
            lease.run_id,
            lease.attempt_id,
            lease.lease_token,
            outcome,
            RunOutcome.UNKNOWN,
            (),
            now=failed_at,
        )
        uow.commit()
```

Catch `OSError` and the store's domain exception around `put()` calls, invoke this
helper, return `True`, and keep the existing staging/workspace `finally` cleanup.

- [ ] **Step 3: Write latest-Attempt API RED test**

Create a fake Run with two Attempts, attach one failed case/metric/gate to each
Attempt, invoke `SqlAlchemyRunReader.get_run`, and assert the top-level collections
use only the highest `attempt_no` while artifacts include both Attempt IDs.

Run: `python -m pytest tests/unit/test_api_runs.py -q`

Expected: FAIL because the reader currently loads all Attempt results together.

- [ ] **Step 4: Filter public aggregates to the latest Attempt**

Replace the all-Attempt query IDs with:

```python
attempt_ids = [attempt.attempt_id for attempt in run.attempts]
latest_attempt_id = run.attempts[-1].attempt_id if run.attempts else None

if latest_attempt_id is not None:
    run.case_results = list(session.scalars(
        select(CaseResult).where(CaseResult.attempt_id == latest_attempt_id)
    ))
    run.metrics = list(session.scalars(
        select(Metric).where(Metric.attempt_id == latest_attempt_id)
    ))
    run.gates = list(session.scalars(
        select(GateEvaluation).where(GateEvaluation.attempt_id == latest_attempt_id)
    ))
    run.artifacts = list(session.scalars(
        select(Artifact).where(Artifact.attempt_id.in_(attempt_ids))
    ))
else:
    run.case_results = []
    run.metrics = []
    run.gates = []
    run.artifacts = []
```

- [ ] **Step 5: Run focused tests**

Run:

```powershell
python -m pytest tests/unit/test_api_runs.py -q
$env:DATABASE_URL='postgresql+psycopg://quality_flow@127.0.0.1:55432/quality_flow'
python -m pytest tests/integration/test_worker_lifecycle.py -q -k 'artifact or retry'
```

Expected: all selected tests PASS.

- [ ] **Step 6: Commit Task 3**

```bash
git add src/quality_flow/application/worker.py src/quality_flow/api/dependencies.py tests/unit/test_api_runs.py tests/integration/test_worker_lifecycle.py
git commit -m "fix: preserve retry failure boundaries"
```

---

### Task 4: Documentation and Full Delivery Verification

**Files:**
- Modify: `README.md`
- Test: all existing test layers and delivery contracts.

**Interfaces:**
- Consumes: completed controlled retry behavior.
- Produces: truthful user and interview documentation.

- [ ] **Step 1: Document the feature and its boundary**

Add a short README section covering:

```markdown
### Controlled automatic retry

Suites may opt into one retry for an expired Worker lease. PostgreSQL atomically
marks Attempt 1 abandoned, returns the same Run to queued, records
`run.retry_scheduled`, and creates a new Outbox event. Attempt 2 receives a new
lease and the API preserves both Attempt records. Test failures, quality-gate
failures, timeouts, configuration/result errors, and artifact failures are not
retried. Restful Booker keeps this policy disabled because it writes to a shared
external API.
```

Add an interview answer: “The platform does not blindly rerun failed tests. It
only retries a Worker-loss infrastructure failure once, keeps both Attempts, and
uses PostgreSQL transactions, Run locking, and lease fencing to prevent duplicate
effective execution and stale result overwrite.”

- [ ] **Step 2: Run formatting, unit, and integration verification**

Run:

```powershell
python -m ruff check .
python -m pytest tests/unit -q
$env:DATABASE_URL='postgresql+psycopg://quality_flow@127.0.0.1:55432/quality_flow'
$env:TASK7_REDIS_URL='redis://127.0.0.1:6379/14'
python -m pytest tests/integration -q
python -m compileall -q src tests
python -m pip check
git diff --check
```

Expected: every command exits 0; only existing platform-dependent skips/warnings
are allowed.

- [ ] **Step 3: Run deterministic stack verification**

Run the repository's documented Compose/E2E workflow with the exact project name,
then run:

```powershell
python -m pytest tests/e2e -q
```

Expected: all seven deterministic E2E scenarios PASS, all long-running services
remain healthy, and cleanup is limited to the verified QualityFlow Compose project.

- [ ] **Step 4: Inspect final history and commit documentation**

```bash
git add README.md
git commit -m "docs: explain controlled worker retry"
git status --short
git log -5 --oneline
```

Expected: clean working tree with focused feature commits.

- [ ] **Step 5: Request independent review**

Have a read-only reviewer verify policy scope, retry budget, Reconciler races,
artifact failure handling, API aggregation, tests, and README claims. Fix any
confirmed Critical or Important finding with a new RED test before final delivery.
