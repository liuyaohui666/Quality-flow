# Formal Console UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the existing framework-free QualityFlow console into a formal enterprise test-operations interface while preserving all API behavior.

**Architecture:** Keep the current HTML/CSS/JavaScript single page and enhance its semantic landmarks, client-derived overview metrics, responsive grids, and tokenized visual system. No backend endpoint or database change is required.

**Tech Stack:** Semantic HTML, CSS custom properties, vanilla JavaScript, FastAPI static files, pytest.

## Global Constraints

- Do not add a Node.js build chain or frontend framework.
- Preserve every existing API route and interactive element ID.
- Use inline SVG icons rather than emoji or font glyphs.
- Maintain 44px targets, visible focus, responsive reflow, and reduced-motion support.
- Do not expose commands, paths, credentials, or new privileged controls.

---

### Task 1: Formal shell and operational overview

**Files:**
- Modify: `src/quality_flow/api/static/index.html`
- Modify: `src/quality_flow/api/static/styles.css`
- Modify: `src/quality_flow/api/static/app.js`
- Test: `tests/unit/test_api_console.py`

**Interfaces:**
- Consumes: existing `state.runs`, navigation handlers, suite catalog
- Produces: `overview-total`, `overview-running`, `overview-passed`, `overview-attention`, `renderOverview()`

- [ ] Add a failing contract test for the environment indicator, four KPI IDs, consistent inline SVG icons, and `renderOverview`.
- [ ] Run the focused test and confirm it fails because the formal landmarks do not exist.
- [ ] Restructure the shell and runs view without changing existing control IDs.
- [ ] Add `renderOverview()` and call it after run data loads.
- [ ] Replace the visual system with tokenized enterprise surfaces, type scale, controls, table states, and responsive behavior.
- [ ] Run the focused test and JavaScript syntax check, then commit.

### Task 2: Execution workspace and evidence views

**Files:**
- Modify: `src/quality_flow/api/static/index.html`
- Modify: `src/quality_flow/api/static/styles.css`
- Modify: `src/quality_flow/api/static/app.js`
- Test: `tests/unit/test_api_console.py`

**Interfaces:**
- Consumes: selected test type, suite, parameters, request-body validation state
- Produces: `execution-summary`, `summary-test-type`, `summary-suite`, `updateExecutionSummary()`

- [ ] Add a failing contract test for the two-column workspace and execution summary IDs.
- [ ] Run the focused test and confirm the missing elements fail.
- [ ] Recompose the create view and detail/health panels while preserving form submission and polling.
- [ ] Update the summary whenever type, suite, or JSON validation changes.
- [ ] Run the console tests and JavaScript syntax check, then commit.

### Task 3: Accessibility, responsive QA, and delivery

**Files:**
- Modify: `README.md`
- Test: `tests/unit/test_api_console.py`

**Interfaces:**
- Demonstrates: a keyboard-accessible, responsive formal control plane

- [ ] Document the upgraded console and its unchanged trust boundary.
- [ ] Run Ruff, JavaScript syntax, console tests, and the full unit suite.
- [ ] Inspect at desktop, tablet, and 375px widths plus reduced-motion/focus states.
- [ ] Review the complete diff for API compatibility and accidental secret/path exposure.
- [ ] Commit, merge locally to `main`, and do not push without user authorization.

