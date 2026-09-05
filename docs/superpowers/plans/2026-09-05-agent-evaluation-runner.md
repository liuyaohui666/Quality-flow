# Agent Evaluation Runner Implementation Plan

## Goal

Build and verify one rule-based, deterministic Agent application evaluation vertical slice.

## Tasks

1. Add generic `MetricData` to `RunnerOutcome` and persist it through the existing repository.
2. Parse strict immutable `agent_eval` YAML definitions and reject unknown or unsafe fields.
3. Execute cases with bounded HTTP requests, rule-based Agent assertions, functional gating, generic metrics, and a sanitized JSON Artifact.
4. Register `agent_eval` in the suite registry and default Worker factory.
5. Add the deterministic Demo Target endpoint and a three-case registered suite.
6. Add unit and E2E coverage, document truthful scope and limitations, then run Ruff, all unit tests, PostgreSQL/Redis integration tests, and Compose E2E.

Every implementation task starts with a failing focused test. No real model API or secret is required.
