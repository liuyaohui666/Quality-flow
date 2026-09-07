# DeepSeek Agent Target Design

## Goal

Add a real model-backed Agent application to QualityFlow so the existing Agent Eval Runner can evaluate an actual multi-turn tool-using system without making CI depend on a paid external API.

## Architecture

The existing deterministic `/agent/respond` target remains unchanged for reliable CI and regression demonstrations. A new `/agent/deepseek/respond` endpoint delegates planning to DeepSeek, validates model-generated tool calls against an allowlist and JSON Schema, executes only local side-effect-free tools, returns tool results to the model, and exposes the same structured response contract already consumed by `AgentEvalRunner`.

The real path is:

`AgentEvalRunner -> demo-target DeepSeek endpoint -> DeepSeek Chat Completions -> validated local tool -> DeepSeek final answer -> evaluation contract`.

## Security and cost boundaries

- Read `DEEPSEEK_API_KEY` only from process environment.
- Never put the key in Git, HTTP response bodies, logs, artifacts, or the browser.
- Inject the key only into the `demo-target` container.
- Keep real API execution out of GitHub Actions and the default deterministic end-to-end suite.
- Limit model/tool iterations, output tokens, request duration, and accepted response size.
- Expose only `weather.lookup` and `calendar.create`; both are deterministic local sandbox tools and have no external side effects.
- Validate every tool argument before execution and reject malformed or unknown calls.

## Failure behavior

- Missing configuration returns HTTP 503 with a stable non-secret message.
- Provider authentication, rate limiting, timeout, malformed JSON, and protocol failures return HTTP 502/504 without leaking upstream bodies or credentials.
- Invalid tool calls stop the loop and return a contract-valid refusal with a recorded, redacted tool trace.
- Exceeding the tool-round limit returns a contract-valid refusal.

## Evaluation and CI

- Add `deepseek-agent-eval` as a manually runnable registered suite.
- Use a separate YAML definition with realistic but bounded expectations.
- Unit tests use injected fake transports/providers and never spend API credit.
- Existing deterministic Agent tests remain the default CI evidence.
