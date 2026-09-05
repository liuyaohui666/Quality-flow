# Agent Tool Argument Contracts Design

## Goal

Extend `agent_eval` so it can prove that an Agent not only selects an allowed tool, but also supplies arguments that conform to that tool's trusted JSON Schema contract.

## Configuration

Each turn expectation may add:

```yaml
tool_argument_schemas:
  weather.lookup:
    type: object
    required: [city]
    properties:
      city: {type: string, minLength: 1}
    additionalProperties: false
max_tool_calls: 1
```

`tool_argument_schemas` defaults to an empty mapping. Every key must be a non-empty tool name already present in `allowed_tools`, every schema must be a mapping, and every schema must pass JSON Schema Draft 2020-12 schema validation when the trusted YAML is loaded. `max_tool_calls` is optional and, when present, is an integer from 0 through 100.

The parsed top-level schema mapping is copied and exposed read-only so later callers cannot replace a contract after registry loading.

## Evaluation

For every returned tool call whose name has a configured schema, the runner validates its `arguments`. A turn fails when any argument object violates its contract or when the response contains more calls than `max_tool_calls`.

Validation messages identify the tool, call position, failing argument path, and JSON Schema keyword. They do not echo the rejected value, which avoids copying secrets into case messages. The sanitized response remains available in the bounded JSON report for authorized diagnosis.

Argument-contract and call-count failures count as tool-policy violations, so they contribute to the existing `tool_violation_rate`. They also naturally reduce sample pass rate when stability sampling is enabled. Existing definitions without these fields remain unchanged.

## Demo and Verification

The local Agent demo adds strict contracts for `weather.lookup` (`city`) and `calendar.create` (`region`) and permits one tool call per corresponding turn. Unit tests cover valid parsing, malformed schemas, schemas for non-allowlisted tools, immutability, valid arguments, wrong types, missing required fields, extra fields, repeated same-name calls, redacted failure messages, and interaction with multi-sample evaluation. The end-to-end Agent scenario continues to pass and persist zero tool-policy violations.

## Non-goals

This increment does not execute tools, infer schemas from source code, validate tool return values, accept client-supplied schemas, or introduce an LLM judge.
