# QualityFlow Lightweight Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a lightweight Run-centered Web console, safe result-file access, and a short Windows launcher without changing QualityFlow execution semantics.

**Architecture:** FastAPI continues to own the control plane and directly serves framework-free HTML/CSS/JavaScript. New read-only APIs expose safe suite metadata, Run history, latest-Attempt cases, and database-authorized Artifact bytes; the API container receives the existing Artifact volume read-only. A root PowerShell launcher wraps only fixed, project-scoped Compose operations.

**Tech Stack:** Python 3.12, FastAPI/Starlette, SQLAlchemy 2, Pydantic 2, HTML/CSS/JavaScript, PowerShell 7/Windows PowerShell, Docker Compose, pytest.

## Global Constraints

- Do not add Node.js, React, Vue, WebSocket, SSE, authentication, manual retry, cancellation, deletion, scheduling, DAGs, or arbitrary command execution.
- Never accept an Artifact URI or filesystem path from a client.
- Keep the Worker Artifact mount read-write and mount the same volume into the API read-only.
- `qualityflow.ps1 stop` must preserve all named volumes and must never call prune.
- Existing POST semantics, Outbox/Celery delivery, retry policy, state machine, and CI gate semantics remain unchanged.
- Public APIs must not expose suite argv, working directories, event payload dictionaries, CaseResult details, Artifact URIs, credentials, or internal paths.

---

## File Structure

- Modify `src/quality_flow/suites/registry.py`: return an immutable tuple of registered suite definitions.
- Modify `src/quality_flow/api/dependencies.py`: add Run history queries and inject safe suite/Artifact readers.
- Modify `src/quality_flow/api/schemas.py`: define suite, Run-list, and case response contracts.
- Create `src/quality_flow/api/routes/suites.py`: expose the safe suite catalog.
- Modify `src/quality_flow/api/routes/runs.py`: expose Run list, latest cases, and authorized Artifact content.
- Create `src/quality_flow/api/routes/ui.py`: redirect `/` and serve `/ui/`.
- Create `src/quality_flow/api/static/index.html`: accessible application shell.
- Create `src/quality_flow/api/static/styles.css`: responsive visual system.
- Create `src/quality_flow/api/static/app.js`: API calls, rendering, filtering, polling, and error states.
- Modify `src/quality_flow/api/app.py`: register catalog/UI routes and static assets.
- Modify `compose.yaml`: give API read-only Artifact access.
- Create `qualityflow.ps1`: fixed project-scoped start/stop/status/logs commands.
- Modify `.gitignore` and `.dockerignore`: exclude visual brainstorm state.
- Modify `README.md`: document the normal UI-first workflow.
- Modify/add tests under `tests/unit`, `tests/integration`, and `tests/e2e` for every new boundary.

---

### Task 1: Safe Suite Catalog

**Files:**
- Modify: `src/quality_flow/suites/registry.py`
- Modify: `src/quality_flow/api/dependencies.py`
- Modify: `src/quality_flow/api/schemas.py`
- Create: `src/quality_flow/api/routes/suites.py`
- Modify: `src/quality_flow/api/app.py`
- Test: `tests/unit/test_api_console.py`

**Interfaces:**
- Produces: `SuiteRegistry.all() -> tuple[SuiteDefinition, ...]`
- Produces: `GET /api/v1/suites -> SuitesResponse`
- Public fields: `suite_id`, `runner_type`, `allowed_parameters`

- [ ] **Step 1: Write failing tests**

```python
def test_suite_catalog_exposes_only_safe_creation_fields(console_client):
    response = console_client.get("/api/v1/suites")
    assert response.status_code == 200
    assert response.json() == {"suites": [{
        "suite_id": "demo-api",
        "runner_type": "pytest",
        "allowed_parameters": {"scenario": ["ok", "error", "slow"]},
    }]}
    assert "argv" not in response.text
    assert "working_directory" not in response.text
```

- [ ] **Step 2: Run the focused test and verify 404 failure**

Run: `python -m pytest tests/unit/test_api_console.py::test_suite_catalog_exposes_only_safe_creation_fields -q`

Expected: FAIL because `/api/v1/suites` does not exist.

- [ ] **Step 3: Implement immutable registry enumeration and safe schemas**

```python
def all(self) -> tuple[SuiteDefinition, ...]:
    return tuple(self._suites[key] for key in sorted(self._suites))

class SuiteResponse(BaseModel):
    suite_id: str
    runner_type: str
    allowed_parameters: dict[str, list[str]]

class SuitesResponse(BaseModel):
    suites: list[SuiteResponse]
```

Store `suite_definitions: tuple[SuiteDefinition, ...] = ()` in `ApiDependencies`, populate it from `registry.all()`, and map only those three fields in the new route.

- [ ] **Step 4: Register the router and run focused tests**

Run: `python -m pytest tests/unit/test_api_console.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/quality_flow/suites/registry.py src/quality_flow/api/dependencies.py src/quality_flow/api/schemas.py src/quality_flow/api/routes/suites.py src/quality_flow/api/app.py tests/unit/test_api_console.py
git commit -m "feat: expose safe suite catalog"
```

### Task 2: Run History and Latest-Attempt Cases

**Files:**
- Modify: `src/quality_flow/api/dependencies.py`
- Modify: `src/quality_flow/api/schemas.py`
- Modify: `src/quality_flow/api/routes/runs.py`
- Test: `tests/unit/test_api_console.py`
- Test: `tests/integration/test_run_persistence.py`

**Interfaces:**
- Produces: `RunReader.list_runs(limit: int, status: RunStatus | None, suite_id: str | None) -> tuple[Any, ...]`
- Produces: `GET /api/v1/runs?limit=50&status=completed&suite_id=demo-api`
- Produces: `GET /api/v1/runs/{run_id}/cases`

- [ ] **Step 1: Write failing API contract tests**

```python
def test_run_history_is_newest_first_and_filterable(console_client):
    response = console_client.get("/api/v1/runs?status=completed&suite_id=demo-api&limit=20")
    assert response.status_code == 200
    assert response.json()["runs"][0]["suite_id"] == "demo-api"

def test_cases_expose_latest_attempt_without_details(console_client):
    response = console_client.get(f"/api/v1/runs/{RUN_ID}/cases")
    assert response.status_code == 200
    assert response.json()["cases"][0]["node_id"] == "tests/test_target.py::test_target"
    assert "details" not in response.text
```

- [ ] **Step 2: Verify focused failures**

Run: `python -m pytest tests/unit/test_api_console.py -q`

Expected: FAIL because `list_runs` and `/cases` are missing.

- [ ] **Step 3: Implement bounded history query**

```python
def list_runs(self, limit: int, status: RunStatus | None, suite_id: str | None) -> tuple[Run, ...]:
    with self._session_factory() as session:
        statement = select(Run).options(selectinload(Run.attempts))
        if status is not None:
            statement = statement.where(Run.status == status)
        if suite_id is not None:
            statement = statement.where(Run.suite_id == suite_id)
        rows = session.scalars(
            statement.order_by(Run.created_at.desc(), Run.run_id.desc()).limit(limit)
        ).all()
        for row in rows:
            session.expunge(row)
        return tuple(rows)
```

Use FastAPI `Query(default=50, ge=1, le=100)` and `RunStatus | None` so invalid states receive 422.

- [ ] **Step 4: Implement response schemas**

```python
class RunSummaryResponse(BaseModel):
    run_id: UUID
    suite_id: str
    status: str
    outcome: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    latest_attempt_no: int | None

class CaseResponse(BaseModel):
    case_result_id: UUID
    attempt_id: UUID
    node_id: str
    status: str
    duration_ms: float | None
    message: str | None
    created_at: datetime
```

Map cases from `_read_run(...).case_results`, which already contains only the latest Attempt.

- [ ] **Step 5: Add a real persistence integration assertion**

Insert two Runs with different timestamps and two Attempt result sets; assert sort order, status filter, and latest-Attempt case selection through `SqlAlchemyRunReader`.

Run: `python -m pytest tests/unit/test_api_console.py tests/integration/test_run_persistence.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/quality_flow/api/dependencies.py src/quality_flow/api/schemas.py src/quality_flow/api/routes/runs.py tests/unit/test_api_console.py tests/integration/test_run_persistence.py
git commit -m "feat: add run history and case queries"
```

### Task 3: Authorized Artifact Viewing and Download

**Files:**
- Modify: `src/quality_flow/api/dependencies.py`
- Modify: `src/quality_flow/api/routes/runs.py`
- Modify: `compose.yaml`
- Test: `tests/unit/test_api_console.py`
- Test: `tests/unit/test_compose_contract.py`
- Test: `tests/e2e/test_quality_flow.py`

**Interfaces:**
- Consumes: `FileArtifactStore.resolve(uri: str) -> Path`
- Produces: `GET /api/v1/runs/{run_id}/artifacts/{artifact_id}/content?download=false`

- [ ] **Step 1: Write ownership and path-boundary tests**

```python
def test_artifact_content_uses_database_owned_uri(console_client):
    response = console_client.get(f"/api/v1/runs/{RUN_ID}/artifacts/{ARTIFACT_ID}/content")
    assert response.status_code == 200
    assert response.content == b"captured output\n"
    assert "inline" in response.headers["content-disposition"]

def test_artifact_from_another_run_is_hidden(console_client):
    response = console_client.get(f"/api/v1/runs/{OTHER_RUN_ID}/artifacts/{ARTIFACT_ID}/content")
    assert response.status_code == 404
    assert "runtime" not in response.text
```

- [ ] **Step 2: Verify focused failures**

Run: `python -m pytest tests/unit/test_api_console.py -q`

Expected: FAIL because the content endpoint is missing.

- [ ] **Step 3: Inject the Artifact store and return FileResponse**

Add `artifact_store: FileArtifactStore | None = None` to `ApiDependencies`. Build it from `QUALITY_FLOW_ARTIFACT_ROOT`, defaulting to `<project>/artifacts` outside Compose. In the route, first load the Run, locate the matching Artifact in `run.artifacts`, then resolve only its stored URI. Catch `ArtifactStoreError` and return a path-free 404.

```python
disposition = "attachment" if download else "inline"
return FileResponse(
    artifact_path,
    media_type=artifact.artifact_metadata.get("mime_type") or "application/octet-stream",
    filename=safe_artifact_filename(artifact.artifact_type),
    content_disposition_type=disposition,
)
```

- [ ] **Step 4: Mount Artifact storage into API read-only**

```yaml
api:
  volumes:
    - quality-flow-artifacts:/runtime/artifacts:ro
```

Keep the Worker mount without `:ro`.

- [ ] **Step 5: Verify Compose and E2E bytes**

Extend E2E to download at least stdout and JUnit from a completed `demo-api / ok` Run and assert non-empty content plus correct ownership.

Run: `python -m pytest tests/unit/test_api_console.py tests/unit/test_compose_contract.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/quality_flow/api/dependencies.py src/quality_flow/api/routes/runs.py compose.yaml tests/unit/test_api_console.py tests/unit/test_compose_contract.py tests/e2e/test_quality_flow.py
git commit -m "feat: add safe artifact content access"
```

### Task 4: Framework-Free Run Console

**Files:**
- Create: `src/quality_flow/api/routes/ui.py`
- Create: `src/quality_flow/api/static/index.html`
- Create: `src/quality_flow/api/static/styles.css`
- Create: `src/quality_flow/api/static/app.js`
- Modify: `src/quality_flow/api/app.py`
- Test: `tests/unit/test_api_console.py`

**Interfaces:**
- Consumes: `/api/v1/suites`, `/api/v1/runs`, `/api/v1/runs/{id}`, `/cases`, `/events`, `/artifacts`, `/content`
- Produces: `/ -> 307 /ui/`, `/ui/`, `/ui/assets/styles.css`, `/ui/assets/app.js`

- [ ] **Step 1: Write failing UI route and asset tests**

```python
def test_ui_shell_and_assets_are_served(console_client):
    root = console_client.get("/", follow_redirects=False)
    page = console_client.get("/ui/")
    script = console_client.get("/ui/assets/app.js")
    assert root.headers["location"] == "/ui/"
    assert page.status_code == script.status_code == 200
    assert "QualityFlow" in page.text
    assert "createRun" in script.text
```

- [ ] **Step 2: Verify 404 failure**

Run: `python -m pytest tests/unit/test_api_console.py::test_ui_shell_and_assets_are_served -q`

Expected: FAIL because UI routes do not exist.

- [ ] **Step 3: Add the application shell**

The HTML must include semantic navigation, `aria-live` status output, create form, Run table, detail tabs, loading/empty/error panels, and `<dialog>` or an equivalent accessible detail surface. It must reference only `/ui/assets/styles.css` and `/ui/assets/app.js`; no CDN is permitted.

- [ ] **Step 4: Implement API-driven behavior**

```javascript
async function api(path, options = {}) {
  const response = await fetch(path, {headers: {"Content-Type": "application/json", ...(options.headers || {})}, ...options});
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`);
  return response.json();
}

async function createRun(payload) {
  return api("/api/v1/runs", {
    method: "POST",
    headers: {"Idempotency-Key": crypto.randomUUID()},
    body: JSON.stringify(payload),
  });
}
```

Render text with `textContent`, not `innerHTML`, for API-derived values. Poll queued/running detail every two seconds, stop at terminal state, and pause while `document.hidden` is true.

- [ ] **Step 5: Implement responsive, high-contrast styles**

Use a dark navy sidebar, neutral content surface, status badges that include text as well as color, visible focus rings, 44px minimum interactive targets, and a one-column layout below 760px.

- [ ] **Step 6: Run route tests and browser smoke test**

Run: `python -m pytest tests/unit/test_api_console.py -q`

Expected: PASS. Then serve the app and verify create/list/detail/artifact interactions in a real browser.

- [ ] **Step 7: Commit**

```bash
git add src/quality_flow/api/routes/ui.py src/quality_flow/api/static src/quality_flow/api/app.py tests/unit/test_api_console.py
git commit -m "feat: add lightweight run console"
```

### Task 5: Short Windows Launcher

**Files:**
- Create: `qualityflow.ps1`
- Create: `tests/unit/test_launcher_contract.py`
- Modify: `.gitignore`
- Modify: `.dockerignore`

**Interfaces:**
- Produces: `.\qualityflow.ps1 start|stop|status|logs`

- [ ] **Step 1: Write failing launcher safety contract**

```python
def test_launcher_is_project_scoped_and_non_destructive():
    script = (PROJECT_ROOT / "qualityflow.ps1").read_text(encoding="utf-8")
    assert "quality-flow-demo" in script
    assert "--remove-orphans" in script
    assert "prune" not in script.lower()
    assert not re.search(r"\bdown\b[^\n]*\s-v(?:\s|$)", script)
```

- [ ] **Step 2: Verify missing-file failure**

Run: `python -m pytest tests/unit/test_launcher_contract.py -q`

Expected: FAIL because `qualityflow.ps1` does not exist.

- [ ] **Step 3: Implement fixed launcher actions**

```powershell
param([ValidateSet("start", "stop", "status", "logs")][string]$Action = "start")
$compose = Join-Path $PSScriptRoot "compose.yaml"
$baseArgs = @("compose", "-p", "quality-flow-demo", "-f", $compose)
switch ($Action) {
  "start" { & docker @baseArgs "up" "-d" "--build" "--wait" "--wait-timeout" "180" }
  "stop" { & docker @baseArgs "down" "--remove-orphans" }
  "status" { & docker @baseArgs "ps" "--all" }
  "logs" { & docker @baseArgs "logs" "--no-color" "--tail" "200" }
}
```

Add Docker availability/error handling, preserve non-zero exit codes, and open `http://127.0.0.1:18000/ui/` only after successful readiness.

- [ ] **Step 4: Ignore local visual-companion state**

Add `.superpowers/` to both `.gitignore` and `.dockerignore` so local design mockups never enter commits or build context.

- [ ] **Step 5: Run launcher and Compose contract tests**

Run: `python -m pytest tests/unit/test_launcher_contract.py tests/unit/test_compose_contract.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add qualityflow.ps1 tests/unit/test_launcher_contract.py .gitignore .dockerignore
git commit -m "feat: add project-scoped launcher"
```

### Task 6: Documentation and Full Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/evidence-matrix.md`
- Modify: `.github/workflows/quality-flow.yml` only if new E2E invocation needs explicit collection

**Interfaces:**
- Documents: UI-first start, result viewing, API fallback, non-destructive stop, and known limits.

- [ ] **Step 1: Replace the primary manual workflow**

Document `.\qualityflow.ps1 start`, browser creation and result viewing first. Keep raw Docker/API commands under an “advanced diagnostics and automation” section instead of deleting them.

- [ ] **Step 2: Update evidence mapping**

Map suite catalog, Run history, latest cases, Artifact ownership/download, UI serving, launcher safety, and browser E2E to exact test files and CI jobs.

- [ ] **Step 3: Run static and unit verification**

Run:

```powershell
python -m ruff check .
python -m pytest tests/unit -q
docker compose -p quality-flow-demo config --quiet
```

Expected: all commands exit 0.

- [ ] **Step 4: Rebuild and run integration/E2E**

Use the repository's existing isolated integration command from `.github/workflows/quality-flow.yml`, then:

```powershell
.\qualityflow.ps1 start
$env:QUALITY_FLOW_API_URL = "http://127.0.0.1:18000"
python -m pytest tests/e2e -q
```

Expected: services healthy and all integration/E2E tests pass.

- [ ] **Step 5: Perform browser verification**

Open `/ui/`, create `demo-api / ok`, observe queued/running/terminal rendering, inspect cases and events, open stdout inline, download JUnit, resize below 760px, and verify no console errors.

- [ ] **Step 6: Run diff and secret review**

Run:

```powershell
git diff --check
git status --short
rg -n "Authorization|Bearer |password=|postgresql://" src/quality_flow/api/static qualityflow.ps1
```

Expected: no whitespace errors, no unexpected files, and no secret-like frontend/launcher content.

- [ ] **Step 7: Commit documentation**

```bash
git add README.md docs/evidence-matrix.md .github/workflows/quality-flow.yml
git commit -m "docs: explain ui-first quality flow workflow"
```

- [ ] **Step 8: Final verification record**

Record exact test counts and any environment-limited checks in the final handoff. Do not claim GitHub-hosted CI is green unless a new pushed workflow run has actually completed successfully.
