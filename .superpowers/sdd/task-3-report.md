# Task 3 Report: Restful Booker runner contract

## Scope delivered

- Added `tests/unit/test_restful_booker_suite.py`.
- The test copies `demo_suites/restful_booker` into `tmp_path/workspace` while
  excluding generated caches and `logs`.
- It creates the offline four-file pytest `ExecutionSpec` using the repository
  registry's Restful Booker timeout, empty parameter resolution and functional
  gate policy.
- It executes the real `PytestRunner` with artifacts staged exclusively under
  `tmp_path/staging` and verifies passed attempt status, parsed JUnit summary,
  a passed gate and the `stdout`, `stderr`, and `junit_xml` artifacts.

No runner, suite, retry, network, documentation, or Task 4+ behavior changed.
All workspace, JUnit result-directory and staging output are created inside
pytest's temporary directory; the repository's suite `logs` directory is not
copied or written by this regression test.

## TDD observation

The new runner-contract test was executed immediately after being added. It
passed on its first run (`1 passed in 0.89s`) because the prior Task 2 suite
implementation and `requests` dependency were already present in this
environment. Therefore the planned missing-contract RED state was not
reproducible, and no contract-level production repair was justified.

## Verification evidence

| Command | Result |
| --- | --- |
| `python -m pytest tests/unit/test_restful_booker_suite.py -q` | `1 passed in 0.89s` |
| `python -m pytest tests/unit/test_restful_booker_suite.py tests/unit/test_suite_registry.py -q` | `8 passed in 0.81s` |
| `python -m pytest tests/unit -q` | `205 passed, 5 skipped, 1 warning in 42.94s` |
| `python -m ruff check .` | `All checks passed!` |
| `python -m pip check` | `No broken requirements found.` |
| `git diff --check` | exit 0 |

The unit-suite warning is an existing FastAPI/Starlette `TestClient` deprecation
warning from installed `httpx`; it is unrelated to this task.

## Self-review

- The execution targets only offline unit files; it does not invoke the public
  Restful Booker endpoint or CRUD tests.
- `allowed_workspace_root=tmp_path` encloses the copied workspace, and
  `staging_root=tmp_path / "staging"` is outside that workspace.
- The test uses the real registry, `ExecutionSpec`, runner, JUnit parser, gate
  and artifact generation path rather than mocks.
- The only deliverables are this report and the Task 3 regression test.
