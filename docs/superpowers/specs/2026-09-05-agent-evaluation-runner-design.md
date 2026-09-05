# Agent Evaluation Runner Design

## Goal

Add a small, deterministic Agent application evaluation capability on top of QualityFlow. The platform should evaluate structured Agent responses, tool-use boundaries, scope control, latency, and token usage without requiring a paid model or making unverifiable semantic-quality claims.

## Scope

The first release adds a trusted `agent_eval` runner type. A registered YAML file defines an HTTP endpoint and a list of evaluation cases. Each case provides a prompt and rule-based expectations:

- expected HTTP status and decision (`answer`, `tool_call`, or `refuse`);
- required answer fragments;
- allowed and required tool names;
- whether scope expansion is forbidden;
- maximum output-token count.

The target response contract is intentionally explicit:

```json
{
  "decision": "tool_call",
  "answer": "I will look up the weather.",
  "tool_calls": [{"name": "weather.lookup", "arguments": {"city": "Hefei"}}],
  "scope_expanded": false,
  "usage": {"input_tokens": 8, "output_tokens": 12}
}
```

Each case becomes a normal QualityFlow `CaseResult`. Generic metrics record pass rate, tool-violation rate, P95 latency, and total token usage. A functional gate decides the Run outcome. A sanitized JSON report is stored as an Artifact.

## Trust and execution boundary

Agent evaluation files are trusted repository assets selected through `config/suites.yaml`. API users cannot provide an endpoint, evaluator code, or YAML definition. The endpoint base URL comes only from the worker's allowlisted environment mapping, and the definition supplies a relative path. Reports redact sensitive key names and cap response bodies.

## Deterministic demo

The local Demo Target exposes a small structured Agent-like endpoint with three repeatable behaviors:

1. a normal answer;
2. an allowed weather tool call;
3. refusal of an instruction-injection attempt that requests a destructive admin tool.

This is a test target, not a real language model. It exists so unit, integration, E2E, and CI remain deterministic.

## Failure semantics

- A malformed trusted definition is `infra_failed/unknown`.
- Transport errors, invalid structured responses, policy violations, or failed expectations are `completed/failed`.
- Exhausting the Run deadline is `timed_out/unknown`.
- A report-persistence failure is `infra_failed/unknown`.

## Explicit non-goals

This release does not include an LLM-as-judge, embeddings, semantic similarity, RAG retrieval metrics, stochastic multi-sample confidence intervals, production credentials, prompt optimization, or a visual evaluation editor. Those can build on the same case, metric, gate, and Artifact model later.
