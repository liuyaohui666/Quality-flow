# Task 1 Report: Registry and Dependency Contract

## Scope

Implemented only the Task 1 registration and dependency contract. No Restful Booker
suite directory, test implementation, workflow change, or Task 2+ work was added.

## RED

Command:

```powershell
python -m pytest tests/unit/test_suite_registry.py tests/unit/test_delivery_contracts.py -q
```

Output:

```text
...F....F........                                                        [100%]
FAILED tests/unit/test_suite_registry.py::test_repository_registry_registers_restful_booker_api
quality_flow.suites.registry.UnknownSuiteError: Unknown suite: restful-booker-api
FAILED tests/unit/test_delivery_contracts.py::test_restful_booker_delivery_dependency_and_e2e_boundary
AssertionError: assert 'requests>=2.31,<3' in [...]
2 failed, 15 passed in 0.28s
```

## GREEN

Command:

```powershell
python -m pytest tests/unit/test_suite_registry.py tests/unit/test_delivery_contracts.py -q
```

Output:

```text
.................                                                        [100%]
17 passed in 0.14s
```

Lint command:

```powershell
python -m ruff check tests/unit/test_suite_registry.py tests/unit/test_delivery_contracts.py
```

Output:

```text
All checks passed!
```

## Files

- `tests/unit/test_suite_registry.py`: registry contract for `restful-booker-api`.
- `tests/unit/test_delivery_contracts.py`: `requests` dependency and E2E boundary contract.
- `config/suites.yaml`: exact Restful Booker suite registration.
- `pyproject.toml`: `requests>=2.31,<3` runtime dependency.

## Commit

- Implementation: `8a1fc95812897e69e89e254e1b84c78f39f7ad6d` (`feat: register Restful Booker suite contract`)

## Concerns

- The full `tests/unit` run exceeded the desktop command-output window before a
  final result was returned; the target Task 1 contract suite was rerun fresh and
  passed. This report does not claim the full suite passed.
- `demo_suites/restful_booker` intentionally does not exist yet; Task 1 permits
  this because registry parsing does not execute the suite.
