# QualityFlow API Unit-of-Work Concurrency Design

## 1. Goal

Make one application-wide `RunService` safe to call from concurrent HTTP
requests by giving every `create_run()` call its own SQLAlchemy Unit of Work and
Session.

The change is deliberately internal. `POST /api/v1/runs`, its request and
response models, PostgreSQL schema, idempotency semantics, Outbox behavior, and
worker execution flow remain unchanged.

## 2. Confirmed Root Cause

`build_dependencies()` creates one `RunService` for the lifetime of the FastAPI
application. That service currently owns one `SqlAlchemyUnitOfWork` instance.
`SqlAlchemyUnitOfWork.__enter__()` writes a newly created `session` and
`RunRepository` onto that shared object, and `__exit__()` later closes whatever
Session is currently stored there.

Two overlapping calls can therefore interleave as follows:

```text
request A: shared_uow.session = session_A
request B: shared_uow.session = session_B
request A: add/commit/close through shared_uow.session (now session_B)
```

This can mix two Runs, RunEvents, and OutboxEvents into one transaction, close
another request's Session, leak the overwritten Session, or return a database
error as HTTP 500. The existing concurrent-idempotency integration test did not
cover this lifecycle because it created a separate `RunService` and UoW for each
thread.

## 3. Selected Design: Inject a UoW Factory

`RunService` will receive a callable that creates a `RunUnitOfWork` instead of a
prebuilt Unit of Work:

```python
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

Production assembly will use `functools.partial`:

```python
RunService(partial(SqlAlchemyUnitOfWork, session_factory), registry)
```

Each call now receives a distinct UoW, Session, repository, commit/rollback
boundary, and cleanup path. The application may continue sharing the stateless
`RunService` and immutable `SuiteRegistry`.

The factory contract is intentionally strict: every invocation must return a
new UoW. Supporting both instances and factories would retain the unsafe usage
and make the lifecycle ambiguous.

## 4. Alternatives Rejected

### Construct RunService inside every FastAPI request

This would protect the current HTTP route, but it couples transaction lifetime
to FastAPI dependency wiring and leaves `RunService` unsafe when reused from a
different entry point. The service should express its own per-operation
transaction boundary.

### Make SqlAlchemyUnitOfWork reentrant with thread-local state

Thread-local or context-variable storage could recover the matching Session in
`__exit__()`, but it adds hidden state and difficult nested/async semantics.
Creating a short-lived UoW is simpler and matches the existing Worker, Outbox,
Reader, and Reconciler patterns.

### Serialize calls with a lock

A lock would hide the race by forcing all Run submissions through one request at
a time. It would reduce throughput and preserve the fragile shared-state model,
so it is rejected.

## 5. Transaction and Failure Semantics

Validation of suite IDs and allowed parameters still occurs before opening a
transaction. Once validation succeeds, one factory invocation owns the complete
creation operation:

1. look up the idempotency key;
2. add Run, RunEvent, and OutboxEvent;
3. commit all three atomically;
4. on a unique-key race, roll back and read the winning Run using the same
   request-owned Session;
5. close only that request's Session.

This change does not alter PostgreSQL isolation levels or replace the unique
constraint. The database constraint remains the final guard for simultaneous
first submissions using the same idempotency key.

## 6. Verification

Tests will prove three layers:

- service contract: a shared `RunService` asks its factory for a fresh UoW on
  every `create_run()` call;
- production assembly: `build_dependencies()` passes a callable whose repeated
  invocations return distinct `SqlAlchemyUnitOfWork` instances;
- real PostgreSQL concurrency: two threads share one `RunService`, race on the
  same idempotency key using distinct Sessions, and persist exactly one Run, one
  RunEvent, and one OutboxEvent.

All existing constructor call sites will move to factories. Unit tests, Ruff,
PostgreSQL/Redis integration tests, Docker Compose end-to-end tests, and GitHub
Actions must remain green.

## 7. Acceptance Criteria

- A shared `RunService` never reuses a mutable UoW between calls.
- Concurrent API submissions cannot overwrite or close each other's Session.
- Duplicate idempotency keys still return one persisted Run.
- Run, RunEvent, and OutboxEvent remain one atomic transaction.
- The public HTTP API and database schema do not change.
- No lock, thread-local state, retry, or new dependency is introduced.
