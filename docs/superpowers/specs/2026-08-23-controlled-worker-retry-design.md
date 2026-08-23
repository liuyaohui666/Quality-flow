# QualityFlow Controlled Worker Retry Design

## 1. Goal

QualityFlow V1 currently detects an expired Worker lease and closes the Run as an
infrastructure failure. V1.1 will add one conservative automatic-retry path so the
platform can recover from a Worker that disappears while executing a suite.

The feature is intentionally small and explainable:

- the same Run may have at most two Attempts;
- only an expired Worker lease may trigger the second Attempt;
- test failures, gate failures, timeouts, configuration errors, parsing errors,
  and artifact errors are not retried;
- every Attempt remains visible, so retrying never hides the original failure.

This is a reliability feature, not a general-purpose retry engine.

## 2. Alternatives Considered

### Celery task retry

Using `self.retry()` would be a small code change, but Celery would own retry
counting while PostgreSQL owns Run and Attempt state. That split would make retry
history, terminal state, and failure classification difficult to keep consistent.
This option is rejected.

### A new `retrying` Run status and retry scheduler

This would make the waiting state explicit, but it would add a new state, a new
scheduler responsibility, API changes, and more race conditions. The project does
not need delayed or configurable backoff in V1.1. This option is rejected.

### PostgreSQL-authoritative immediate requeue

The Reconciler will end the expired Attempt and, in the same PostgreSQL
transaction, return the Run to `queued`, append a retry event, and insert a new
Outbox event. The existing Dispatcher, Celery, Redis, and Worker path can then
start Attempt 2. This option reuses existing components and keeps PostgreSQL as
the source of truth. It is selected.

## 3. Suite Retry Policy

`SuiteDefinition` will receive an immutable policy represented in YAML as:

```yaml
retry_policy:
  max_attempts: 2
  retry_on:
    - worker_lost
```

Rules:

- omitted policy means `max_attempts: 1` and no automatic retry;
- V1.1 accepts only `worker_lost` in `retry_on`;
- `max_attempts` may be 1 or 2;
- unknown keys, unknown retry reasons, booleans, and out-of-range values are
  rejected while loading the suite registry;
- the normalized policy is copied into the existing immutable `suite_snapshot`
  when a Run is created, so later YAML changes do not change an existing Run;
- old Run snapshots without a retry policy behave as `max_attempts: 1`.

The local `demo-api` suite will enable the policy so the behavior is demonstrable.
The public Restful Booker suite will keep retries disabled because it performs
write operations against a shared external service and a killed test may already
have produced side effects.

## 4. Retry Lifecycle

The lifecycle is:

```text
Run:       queued -> running -> queued -> running -> terminal
Attempt 1:           running -> abandoned
Attempt 2:                                running -> terminal
```

When the Reconciler finds an expired lease, it locks the Run and Attempt in the
existing order and rechecks the status, lease token, and expiry time.

If the snapshot permits `worker_lost` and the expired Attempt is Attempt 1, one
transaction performs all of the following:

1. change Attempt 1 from `running` to `abandoned`;
2. set its finish time and a safe failure reason indicating lease expiry;
3. change the Run from `running` back to `queued`;
4. keep `outcome=unknown`, keep the original Run `started_at`, and leave
   `finished_at` empty;
5. append one `run.retry_scheduled` RunEvent containing `status=queued`,
   `attempt_no=1`, `next_attempt_no=2`, and `reason=worker_lost`;
6. insert a new unpublished OutboxEvent for the same `run_id`.

The Dispatcher publishes that new event through the existing Celery/Redis path.
The next successful claim creates Attempt 2 with a new Attempt ID and lease token.

If Attempt 2 also loses its lease, or the policy does not permit retry, the
existing terminal behavior is used: the Attempt becomes `abandoned` and the Run
becomes `infra_failed/unknown`. No third Outbox event or Attempt is created.

## 5. Duplicate Messages and Concurrency

V1.1 uses immediate retry and deliberately has no backoff. Therefore an old
redelivered Celery message and the new retry message are equivalent requests to
start the one permitted next Attempt.

Safety comes from existing PostgreSQL coordination plus the retry budget:

- the Worker locks the Run and only claims it while it is `queued`;
- the first message changes it to `running` and creates Attempt 2;
- concurrent or duplicate messages then observe a non-queued Run and do nothing;
- the unique `(run_id, attempt_no)` constraint remains a final database guard;
- two Reconcilers lock and recheck the expired Attempt, so only one can create the
  retry event and Outbox row;
- the existing lease token prevents the old Worker from heartbeating or writing a
  terminal result after Attempt 1 has been abandoned.

Delayed retries, cancellation, replacing one dispatch with another, and more than
two Attempts would require a dispatch-generation fence. They are explicitly
outside this version.

## 6. Artifact Failure Boundary

The current Worker can leave an Attempt in `running` if artifact persistence
raises after the test process has already completed. The Reconciler could then
mistake that state for Worker loss and rerun a suite that already produced
business side effects.

As part of this feature, known artifact persistence failures will be caught while
the lease is still live and recorded immediately as a non-retryable
`infra_failed/unknown` terminal result. The failure summary will be safe and will
not include local paths or credentials. No retry Outbox event is created.

If PostgreSQL itself is unavailable while recording any terminal result, the
platform cannot durably distinguish that situation from a lost Worker. The lease
may eventually expire and use the single retry. Suites that enable Worker-loss
retry must therefore isolate their test data and tolerate at-least-once execution.

## 7. API Semantics

The API continues to expose the same Run statuses. No client must learn a new
`retrying` value.

- `attempts` contains the complete ordered Attempt history;
- `events` contains `run.retry_scheduled` between the two `run.started` events;
- top-level case summary, metrics, and gates describe only the latest Attempt,
  preventing multi-Attempt results from being counted together;
- artifacts remain available across all Attempts and already carry `attempt_id`.

No request field allows a client to override retry policy. Retry behavior remains
an administrator-controlled suite definition.

## 8. Verification

Unit tests will cover:

- retry-policy parsing, defaults, immutability, and invalid values;
- policy snapshotting and backward-compatible old snapshots;
- the `running -> queued` retry transition;
- non-retryable artifact persistence failure;
- latest-Attempt API aggregation.

Real PostgreSQL integration tests will cover:

- Attempt 1 lease expiry creates exactly one retry event and one new Outbox row;
- the next claim creates Attempt 2 and preserves Attempt 1;
- Attempt 2 lease expiry closes the Run without Attempt 3;
- two Reconcilers schedule exactly one retry;
- duplicate delivery creates only one Attempt 2;
- terminal-result and reconciliation races remain mutually exclusive;
- failure during the retry transaction rolls back Run, Attempt, Event, and Outbox
  together.

Existing unit, integration, deterministic end-to-end, Ruff, migration, Docker
Compose, and GitHub Actions checks must remain green. The README will document the
policy, state flow, safety boundary, and a concise interview explanation.

## 9. Acceptance Criteria

- An enabled suite automatically retries once after Worker lease expiry.
- The same Run shows Attempt 1 as `abandoned` and Attempt 2 separately.
- No code path creates Attempt 3.
- Test, gate, timeout, configuration, parsing, and artifact failures do not retry.
- The retry state change and new Outbox event are one PostgreSQL transaction.
- Duplicate messages and concurrent Reconcilers do not create duplicate Attempts.
- A stale Worker cannot overwrite the retried Run.
- Restful Booker remains retry-disabled by default because of external side
  effects.
