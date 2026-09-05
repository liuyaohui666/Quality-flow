# HTTP Workflow Runner Design

## Goal

Add a platform-native, declarative HTTP workflow runner so QualityFlow can execute dependent API steps instead of only launching prewritten pytest or Locust suites.

## First Release Scope

The first release adds one new registered runner type, `workflow`. A trusted YAML definition describes a sequence of HTTP steps. Each step may use values from the Run request body, allowlisted Run parameters, allowlisted worker environment values, or values captured from earlier JSON responses.

The release supports:

- sequential main steps;
- cleanup steps that run after success or failure;
- `GET`, `POST`, `PUT`, `PATCH`, and `DELETE` requests;
- templated paths, headers, query parameters, and JSON bodies;
- expected status codes;
- recursive JSON-subset assertions;
- JSON Schema assertions;
- capture from response JSON using a deliberately small `$.field.nested` path syntax;
- one case result per workflow step;
- the existing functional quality gate;
- a sanitized JSON execution report artifact;
- a deterministic local workflow demo that creates, reads, updates, and deletes a resource.

## Trust Boundary

Workflow files are trusted project files selected through the existing suite registry. API users cannot submit URLs, commands, or workflow definitions. The suite `argv` must have the exact form `workflow <relative-yaml-path>`, and the resolved definition must stay inside the copied attempt workspace.

The base URL is templated from an explicit environment mapping supplied when the worker constructs `WorkflowRunner`. It is not read from arbitrary process environment variables. Request and response reports redact keys containing `authorization`, `cookie`, `password`, `secret`, `token`, or `api_key`, and cap captured body size.

## Definition Format

```yaml
version: 1
name: resource-lifecycle
base_url: "{{ env.QUALITY_FLOW_TARGET_URL }}"
request_timeout_seconds: 5
steps:
  - id: create
    name: Create resource
    request:
      method: POST
      path: /workflow/resources
      json:
        name: "{{ request.name }}"
    expect:
      status: 201
      json_contains:
        name: "{{ request.name }}"
    capture:
      resource_id: $.id
cleanup:
  - id: delete
    name: Delete resource
    when_variables: [resource_id]
    request:
      method: DELETE
      path: "/workflow/resources/{{ resource_id }}"
    expect:
      status: 204
```

Templates may reference `request.<path>`, `parameters.<name>`, `env.<name>`, or a previously captured variable by name. A string consisting only of one template preserves the original value type; interpolation inside a larger string produces text.

## Result Semantics

- A passed assertion creates a `passed` case.
- An unexpected HTTP result creates a `failed` case and stops remaining main steps.
- A transport error creates an `error` case and stops remaining main steps.
- Main steps not reached after failure are recorded as `skipped`.
- Cleanup steps always run in order when their `when_variables` exist; otherwise they are `skipped`.
- Invalid workflow configuration is an infrastructure failure.
- Exceeding the Run deadline is a timed-out attempt.
- The existing functional gate determines the final pass/fail result from step cases.

## Integration

`SuiteRegistry` accepts `workflow` as a runner type while keeping its existing immutable suite snapshot. `build_default_worker` registers `WorkflowRunner` beside pytest and Locust. No database migration is required because step results reuse `CaseResult`, and the report reuses `Artifact`.

The existing operations console already displays suite runner types, cases, gates, and artifact links, so the first release needs no new page. Selecting the workflow suite and submitting its request-body example is enough to demonstrate the full chain.

## Deferred

Branching, loops, parallel steps, OAuth flows, arbitrary JavaScript, user-supplied URLs, a visual workflow editor, OpenAPI import, and LLM judging are intentionally deferred. Agent evaluation will reuse the workflow execution and evidence model in the next release.
