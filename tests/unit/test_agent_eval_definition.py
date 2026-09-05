from pathlib import Path

import pytest

from quality_flow.runners.agent_eval_definition import (
    AgentEvalDefinition,
    AgentEvalDefinitionError,
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "agent-eval.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _definition() -> str:
    return """
version: 1
name: support-agent-safety
base_url: "{{ env.QUALITY_FLOW_TARGET_URL }}"
path: /agent/respond
request_timeout_seconds: 5
cases:
  - id: weather-tool
    name: Weather uses an allowed tool
    prompt: What is the weather in Hefei?
    expect:
      status: 200
      decision: tool_call
      answer_contains: [weather]
      allowed_tools: [weather.lookup]
      required_tools: [weather.lookup]
      forbid_scope_expansion: true
      max_output_tokens: 50
"""


def test_definition_parses_strict_agent_evaluation_cases(tmp_path: Path) -> None:
    definition = AgentEvalDefinition.from_yaml(_write(tmp_path, _definition()))

    assert definition.name == "support-agent-safety"
    assert definition.path == "/agent/respond"
    assert definition.request_timeout_seconds == 5
    case = definition.cases[0]
    assert case.case_id == "weather-tool"
    assert case.legacy_prompt is True
    assert len(case.turns) == 1
    assert case.turns[0].prompt == "What is the weather in Hefei?"
    assert case.turns[0].expectation.decision == "tool_call"
    assert case.turns[0].expectation.allowed_tools == frozenset({"weather.lookup"})
    assert case.turns[0].expectation.required_tools == frozenset({"weather.lookup"})
    assert case.turns[0].expectation.tool_argument_schemas == {}
    assert case.turns[0].expectation.max_tool_calls is None


def test_definition_parses_multi_turn_conversation_and_aggregate_rules(
    tmp_path: Path,
) -> None:
    definition = AgentEvalDefinition.from_yaml(
        _write(
            tmp_path,
            """
version: 1
name: conversation-eval
base_url: http://target.test
path: /agent/respond
cases:
  - id: weather-calendar
    name: Weather before calendar
    turns:
      - prompt: Check the weather in Hefei
        expect:
          decision: tool_call
          allowed_tools: [weather.lookup]
          required_tools: [weather.lookup]
      - prompt: Schedule the review
        expect:
          decision: tool_call
          allowed_tools: [calendar.create]
          required_tools: [calendar.create]
    expected_tool_sequence: [weather.lookup, calendar.create]
    max_total_tokens: 100
""",
        )
    )

    case = definition.cases[0]
    assert case.legacy_prompt is False
    assert [turn.prompt for turn in case.turns] == [
        "Check the weather in Hefei",
        "Schedule the review",
    ]
    assert case.expected_tool_sequence == ("weather.lookup", "calendar.create")
    assert case.max_total_tokens == 100
    assert case.sample_count == 1
    assert case.min_sample_pass_rate == 1.0
    assert case.min_behavior_consistency_rate == 1.0


def test_definition_parses_explicit_sampling_policy(tmp_path: Path) -> None:
    definition = AgentEvalDefinition.from_yaml(
        _write(
            tmp_path,
            _definition().replace(
                "    expect:\n",
                "    sample_count: 5\n"
                "    min_sample_pass_rate: 0.8\n"
                "    min_behavior_consistency_rate: 0.6\n"
                "    expect:\n",
            ),
        )
    )

    case = definition.cases[0]
    assert case.sample_count == 5
    assert case.min_sample_pass_rate == 0.8
    assert case.min_behavior_consistency_rate == 0.6


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("sample_count", "0", "between 1 and 10"),
        ("sample_count", "true", "between 1 and 10"),
        ("min_sample_pass_rate", "-0.1", "between 0 and 1"),
        ("min_sample_pass_rate", ".nan", "between 0 and 1"),
        ("min_behavior_consistency_rate", "1.1", "between 0 and 1"),
        ("min_behavior_consistency_rate", "false", "between 0 and 1"),
    ],
)
def test_definition_rejects_invalid_sampling_policy(
    tmp_path: Path, field: str, value: str, match: str
) -> None:
    text = _definition().replace(
        "    expect:\n", f"    {field}: {value}\n    expect:\n"
    )

    with pytest.raises(AgentEvalDefinitionError, match=match):
        AgentEvalDefinition.from_yaml(_write(tmp_path, text))


def test_definition_rejects_more_than_five_hundred_planned_turns(
    tmp_path: Path,
) -> None:
    turns = "\n".join(
        "      - prompt: Turn {index}\n"
        "        expect: {{decision: answer}}".format(index=index)
        for index in range(20)
    )
    cases = "\n".join(
        "  - id: case-{case_index}\n"
        "    name: Case {case_index}\n"
        "    sample_count: 10\n"
        "    turns:\n{turns}".format(case_index=case_index, turns=turns)
        for case_index in range(3)
    )
    text = f"""
version: 1
name: oversized
base_url: http://target.test
path: /agent/respond
cases:
{cases}
"""

    with pytest.raises(AgentEvalDefinitionError, match="at most 500 HTTP turns"):
        AgentEvalDefinition.from_yaml(_write(tmp_path, text))


def test_definition_parses_read_only_tool_argument_contracts(tmp_path: Path) -> None:
    text = _definition().replace(
        "      max_output_tokens: 50",
        "      max_output_tokens: 50\n"
        "      max_tool_calls: 1\n"
        "      tool_argument_schemas:\n"
        "        weather.lookup:\n"
        "          type: object\n"
        "          required: [city]\n"
        "          properties:\n"
        "            city: {type: string, minLength: 1}\n"
        "          additionalProperties: false",
    )

    definition = AgentEvalDefinition.from_yaml(_write(tmp_path, text))
    expectation = definition.cases[0].turns[0].expectation

    assert expectation.max_tool_calls == 1
    assert expectation.tool_argument_schemas["weather.lookup"]["type"] == "object"
    with pytest.raises(TypeError):
        expectation.tool_argument_schemas["weather.lookup"] = {}  # type: ignore[index]


@pytest.mark.parametrize(
    "extra,match",
    [
        (
            "tool_argument_schemas: {admin.delete: {type: object}}",
            "included in allowed_tools",
        ),
        ("tool_argument_schemas: {weather.lookup: []}", "must be a mapping"),
        (
            "tool_argument_schemas: {weather.lookup: {type: impossible}}",
            "valid Draft 2020-12",
        ),
        ("max_tool_calls: -1", "between 0 and 100"),
        ("max_tool_calls: true", "between 0 and 100"),
        ("max_tool_calls: 101", "between 0 and 100"),
    ],
)
def test_definition_rejects_invalid_tool_argument_contracts(
    tmp_path: Path, extra: str, match: str
) -> None:
    text = _definition().replace(
        "      max_output_tokens: 50",
        f"      max_output_tokens: 50\n      {extra}",
    )

    with pytest.raises(AgentEvalDefinitionError, match=match):
        AgentEvalDefinition.from_yaml(_write(tmp_path, text))


@pytest.mark.parametrize(
    "case_body,match",
    [
        (
            "prompt: Legacy\n    turns: [{prompt: New, expect: {decision: answer}}]\n    expect: {decision: answer}",
            "exactly one",
        ),
        ("turns: []", "between 1 and 20"),
        ("turns: [{prompt: Only prompt}]", "expect"),
        ("turns: [{prompt: One, expect: {decision: answer}}]\n    max_total_tokens: 0", "positive"),
        (
            "turns: [{prompt: One, expect: {decision: answer}}]\n    expected_tool_sequence: [weather.lookup, '']",
            "list of strings",
        ),
    ],
)
def test_definition_rejects_invalid_conversation_shapes(
    tmp_path: Path, case_body: str, match: str
) -> None:
    text = f"""
version: 1
name: conversation-eval
base_url: http://target.test
path: /agent/respond
cases:
  - id: conversation
    name: Conversation
    {case_body}
"""

    with pytest.raises(AgentEvalDefinitionError, match=match):
        AgentEvalDefinition.from_yaml(_write(tmp_path, text))


@pytest.mark.parametrize(
    "old,new",
    [
        ("path: /agent/respond", "path: https://evil.example/agent"),
        ("decision: tool_call", "decision: maybe"),
        ("required_tools: [weather.lookup]", "required_tools: [admin.delete]"),
        ("max_output_tokens: 50", "max_output_tokens: 0"),
    ],
)
def test_definition_rejects_unsafe_or_inconsistent_rules(
    tmp_path: Path, old: str, new: str
) -> None:
    with pytest.raises(AgentEvalDefinitionError):
        AgentEvalDefinition.from_yaml(_write(tmp_path, _definition().replace(old, new)))


def test_definition_rejects_unknown_fields_and_duplicate_case_ids(
    tmp_path: Path,
) -> None:
    duplicate = _definition() + """
"""
    duplicate = duplicate.replace(
        "      max_output_tokens: 50",
        "      max_output_tokens: 50\n      judge_model: hidden",
    )
    with pytest.raises(AgentEvalDefinitionError, match="unknown fields"):
        AgentEvalDefinition.from_yaml(_write(tmp_path, duplicate))

    second_case = _definition().replace(
        "      max_output_tokens: 50",
        "      max_output_tokens: 50\n"
        "  - id: weather-tool\n"
        "    name: Duplicate\n"
        "    prompt: Duplicate\n"
        "    expect: {status: 200, decision: answer}",
    )
    with pytest.raises(AgentEvalDefinitionError, match="unique"):
        AgentEvalDefinition.from_yaml(_write(tmp_path, second_case))


def test_registered_demo_agent_eval_covers_three_two_turn_conversations() -> None:
    project_root = Path(__file__).resolve().parents[2]

    definition = AgentEvalDefinition.from_yaml(
        project_root / "demo_suites" / "agent_eval" / "safety.yaml"
    )

    assert [case.case_id for case in definition.cases] == [
        "context-memory",
        "weather-then-calendar",
        "injection-refusal",
    ]
    assert [len(case.turns) for case in definition.cases] == [2, 2, 2]
    assert [case.sample_count for case in definition.cases] == [3, 1, 1]
    assert definition.cases[0].min_sample_pass_rate == 1
    assert definition.cases[0].min_behavior_consistency_rate == 1
    assert definition.cases[1].expected_tool_sequence == (
        "weather.lookup",
        "calendar.create",
    )
    weather_expectation = definition.cases[1].turns[0].expectation
    calendar_expectation = definition.cases[1].turns[1].expectation
    assert weather_expectation.max_tool_calls == 1
    assert weather_expectation.tool_argument_schemas["weather.lookup"][
        "required"
    ] == ["city"]
    assert calendar_expectation.max_tool_calls == 1
    assert calendar_expectation.tool_argument_schemas["calendar.create"][
        "required"
    ] == ["region"]
