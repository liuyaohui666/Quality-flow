# Agent Conversation Evaluation Design

## Goal

Extend the existing deterministic `agent_eval` runner so QualityFlow can verify multi-turn context retention and ordered tool-use trajectories, while preserving all existing single-prompt evaluation files.

## Chosen approach

Enhance the existing runner instead of introducing a second runner type. A case may use the current `prompt` plus `expect` form or a new `turns` list, but never both. Each turn contains a prompt and the same strict rule-based expectation already used by single-turn cases.

The runner sends a multi-turn case as a `messages` request containing the complete user/assistant history accumulated so far. A single-turn legacy case keeps the existing `{ "prompt": "..." }` request contract. This keeps existing suites compatible and gives new Agent endpoints an explicit conversation contract.

## Definition contract

The multi-turn YAML form is:

```yaml
- id: weather-then-calendar
  name: Check weather before scheduling
  turns:
    - prompt: Check the weather in Hefei
      expect:
        status: 200
        decision: tool_call
        allowed_tools: [weather.lookup]
        required_tools: [weather.lookup]
    - prompt: Schedule a review after that check
      expect:
        status: 200
        decision: tool_call
        allowed_tools: [calendar.create]
        required_tools: [calendar.create]
  expected_tool_sequence: [weather.lookup, calendar.create]
  max_total_tokens: 100
```

Validation remains strict and immutable:

- a case must define exactly one of `prompt` or `turns`;
- a conversation contains between 1 and 20 turns;
- tool-sequence names are non-empty and order-sensitive;
- `max_total_tokens`, when present, is a positive bounded integer;
- existing expectation validation applies independently to each turn.

## Execution and result model

The runner executes turns sequentially. After each response it appends an assistant message containing the returned answer, then sends the accumulated history with the next user prompt. Each turn is checked immediately for response structure, decision, answer fragments, allowed tools, scope expansion, and output-token budget.

One conversation remains one QualityFlow `CaseResult`. It passes only when every turn passes and the aggregate tool sequence and total-token rules pass. The JSON artifact contains a nested `turns` list with each prompt, response, duration, status, and failure message. Existing metrics remain, and `agent_turn_count` records the number of executed HTTP turns.

## Deterministic demo

The local Agent target accepts both the legacy `prompt` request and the new `messages` request. The registered demo covers:

1. remembering a region across two turns;
2. calling `weather.lookup` before `calendar.create`;
3. refusing a destructive prompt injection after an earlier benign turn.

The demo is deterministic and makes no claim about open-ended semantic quality.

## Failure semantics

- malformed YAML remains `infra_failed/unknown`;
- a response or policy mismatch remains `completed/failed`;
- a wrong ordered tool trajectory fails the conversation case;
- a per-request timeout fails the conversation case;
- exhausting the Run deadline marks the Run `timed_out/unknown` and skips untouched cases;
- reports redact sensitive field names and retain the existing body-size bound.

## Non-goals

This increment does not add model-provider credentials, tool execution by QualityFlow, server-side conversation IDs, parallel turns, semantic similarity, LLM-as-judge, RAG evaluation, stochastic sampling, or prompt optimization.
