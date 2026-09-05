# Agent Stability Evaluation Design

## Goal

Extend the existing `agent_eval` runner so one evaluation case can execute the same single-turn or multi-turn conversation several times and measure whether the Agent remains correct and behaviorally consistent.

## Scope

Each case may declare three optional fields:

- `sample_count`: number of independent executions, from 1 through 10; default 1.
- `min_sample_pass_rate`: minimum ratio of samples that must satisfy the existing rules; default 1.0.
- `min_behavior_consistency_rate`: minimum ratio of samples that must share the modal behavior signature; default 1.0.

The parser rejects booleans, non-finite values, out-of-range thresholds, and definitions whose total planned HTTP turns exceed 500. The total is `sample_count * number of turns` summed across all cases.

## Execution Model

Every sample starts with an empty conversation history and executes the full case. Existing per-turn rules, ordered tool-sequence rules, token limits, request timeouts, heartbeat updates, and the overall Run deadline continue to apply.

A behavior signature contains only:

1. the ordered decision values returned by each completed turn;
2. the ordered tool names returned across the conversation.

Answer text is deliberately excluded because wording variation is not itself instability. The behavior consistency rate is the frequency of the most common signature divided by the number of completed samples.

A case passes only when its sample pass rate and behavior consistency rate both meet their configured thresholds. Request-level errors count as failed samples. Exhausting the Run deadline still makes the whole attempt `TIMED_OUT` and stops remaining work.

## Results and Metrics

One configured case remains one QualityFlow `CaseResult`. The JSON report adds a `samples` list with each sample's status, duration, message, decisions, tool sequence, token total, and turn evidence. It also includes the case-level thresholds and observed rates.

For backward compatibility, a one-sample case retains the current top-level `turns`, `actual_tool_sequence`, `total_tokens`, and legacy prompt/response fields.

Existing metrics remain. Three aggregate metrics are added:

- `agent_sample_count`: number of executed samples;
- `agent_sample_pass_rate`: passing samples divided by executed samples;
- `agent_behavior_consistency_rate`: average case consistency weighted by executed samples.

`agent_turn_count` continues to mean actual HTTP turns, so repeated samples increase it.

## Demonstration and Tests

The deterministic demo repeats the context-memory conversation three times and requires 100% correctness and consistency. The other two conversations remain single-sample. The demo therefore executes five samples and ten HTTP turns.

Unit tests use deterministic mock response sequences to prove:

- valid configuration parsing and defaults;
- invalid counts, thresholds, and oversized definitions are rejected;
- repeated conversations start with fresh history;
- pass-rate thresholds can tolerate an explicitly allowed failed sample;
- changing decisions or tool trajectories lowers consistency and can fail a case;
- Run deadline exhaustion still wins over sample aggregation;
- legacy one-sample behavior and report fields remain compatible.

The end-to-end test proves that the platform persists the new metrics and multi-sample report.

## Non-goals

This increment does not connect a paid model provider, compare answer text semantically, use an LLM judge, calculate statistical confidence intervals, or add random behavior to the demo target.
