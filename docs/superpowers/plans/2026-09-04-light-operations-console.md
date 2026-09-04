# Light Operations Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dark compact console with the approved light enterprise operations workspace while preserving all existing platform behavior.

**Architecture:** Keep the zero-build static HTML/CSS/JavaScript frontend and existing FastAPI endpoints. Split the previous combined runs screen into overview and task-list views, then reuse the already-loaded run and suite state to render every new surface.

**Tech Stack:** Semantic HTML, CSS design tokens and media queries, vanilla JavaScript, FastAPI TestClient, pytest.

## Global Constraints

- Do not add a frontend build system or dependency.
- Do not change backend API contracts.
- Do not expose commands, paths, secrets, or arbitrary execution controls.
- Every sidebar destination must work; unsupported enterprise features must not be faked.

---

### Task 1: Formal information architecture

**Files:**
- Modify: `tests/unit/test_api_console.py`
- Modify: `src/quality_flow/api/static/index.html`

**Interfaces:**
- Consumes: existing static asset routes and DOM IDs.
- Produces: `view-overview`, `overview-recent-body`, `view-suites`, and grouped navigation landmarks.

- [ ] Add a failing page-contract test for the new views, grouped navigation, suite catalog, and light-console asset version.
- [ ] Run the focused test and confirm it fails because the landmarks are absent.
- [ ] Restructure the semantic page shell without removing existing form/detail IDs.
- [ ] Run the focused UI tests and confirm they pass.

### Task 2: Shared real-data rendering

**Files:**
- Modify: `src/quality_flow/api/static/app.js`
- Test: `tests/unit/test_api_console.py`

**Interfaces:**
- Consumes: `state.runs`, `state.suites`, `/api/v1/runs`, `/api/v1/suites`.
- Produces: `renderOverviewRecent()` and `renderSuiteCatalog()`.

- [ ] Extend the contract test to require both rendering functions and confirm failure.
- [ ] Render the dashboard recent list and suite directory from existing state.
- [ ] Route overview, runs, suites, create, detail, and health views without duplicate API contracts.
- [ ] Preserve create, polling, filtering, result and Artifact behavior.

### Task 3: Approved light visual system

**Files:**
- Modify: `src/quality_flow/api/static/styles.css`
- Modify: `README.md`

**Interfaces:**
- Consumes: the semantic classes introduced in Task 1.
- Produces: responsive light operations-console presentation.

- [ ] Replace dark tokens with the approved light gray, white, blue, green and semantic status palette.
- [ ] Implement grouped desktop navigation, compact data tables, suite cards, create workspace and responsive layouts.
- [ ] Document the new console organization and launch command.
- [ ] Run focused and full automated verification.

### Task 4: Visual and repository verification

**Files:**
- Verify only.

**Interfaces:**
- Consumes: completed frontend.
- Produces: evidence that the merged main branch is clean and validated.

- [ ] Run `python -m ruff check .` and `node --check src/quality_flow/api/static/app.js`.
- [ ] Run `docker compose -p quality-flow-demo --env-file .env.example config --quiet`.
- [ ] Run `python -m pytest tests/unit -q -p no:cacheprovider`.
- [ ] Preview the host-served UI and inspect desktop and narrow layouts where the environment permits.
- [ ] Commit, fast-forward into local `main`, rerun verification, then remove the owned worktree.
