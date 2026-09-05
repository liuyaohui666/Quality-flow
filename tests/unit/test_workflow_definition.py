from __future__ import annotations

from pathlib import Path

import pytest

from quality_flow.runners.workflow_definition import (
    MissingTemplateVariable,
    WorkflowDefinition,
    WorkflowDefinitionError,
    extract_json_path,
    render_template,
)


def _write_definition(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "workflow.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_definition_parses_dependent_steps_and_cleanup(tmp_path: Path) -> None:
    path = _write_definition(
        tmp_path,
        """
version: 1
name: resource-lifecycle
base_url: "{{ env.QUALITY_FLOW_TARGET_URL }}"
request_timeout_seconds: 4
steps:
  - id: create
    name: Create resource
    request:
      method: POST
      path: /resources
      json:
        name: "{{ request.name }}"
    expect:
      status: 201
      json_contains:
        name: "{{ request.name }}"
    capture:
      resource_id: $.data.id
cleanup:
  - id: delete
    name: Delete resource
    when_variables: [resource_id]
    request:
      method: DELETE
      path: "/resources/{{ resource_id }}"
    expect:
      status: 204
""",
    )

    definition = WorkflowDefinition.from_yaml(path)

    assert definition.name == "resource-lifecycle"
    assert definition.request_timeout_seconds == 4
    assert definition.steps[0].request.method == "POST"
    assert definition.steps[0].capture == {"resource_id": "$.data.id"}
    assert definition.cleanup[0].when_variables == ("resource_id",)


@pytest.mark.parametrize(
    "definition",
    [
        """
version: 1
name: unsafe-method
base_url: http://target
steps:
  - id: invoke
    name: Invoke
    request: {method: CONNECT, path: /}
    expect: {status: 200}
""",
        """
version: 1
name: duplicate-ids
base_url: http://target
steps:
  - id: same
    name: First
    request: {method: GET, path: /one}
    expect: {status: 200}
  - id: same
    name: Second
    request: {method: GET, path: /two}
    expect: {status: 200}
""",
        """
version: 1
name: unknown-fields
base_url: http://target
shell: powershell
steps:
  - id: one
    name: One
    request: {method: GET, path: /}
    expect: {status: 200}
""",
    ],
)
def test_definition_rejects_unsafe_or_ambiguous_input(
    tmp_path: Path, definition: str
) -> None:
    with pytest.raises(WorkflowDefinitionError):
        WorkflowDefinition.from_yaml(_write_definition(tmp_path, definition))


def test_templates_preserve_exact_value_types_and_interpolate_text() -> None:
    context = {
        "request": {"price": 188, "customer": {"name": "Ada"}},
        "parameters": {"scenario": "smoke"},
        "env": {"TARGET": "http://target"},
        "resource_id": 42,
    }

    assert render_template("{{ request.price }}", context) == 188
    assert render_template("/resources/{{ resource_id }}", context) == "/resources/42"
    assert render_template(
        {"name": "{{ request.customer.name }}", "mode": "{{ parameters.scenario }}"},
        context,
    ) == {"name": "Ada", "mode": "smoke"}


def test_template_rejects_missing_or_disallowed_namespaces() -> None:
    with pytest.raises(MissingTemplateVariable):
        render_template("{{ request.missing }}", {"request": {}})

    with pytest.raises(WorkflowDefinitionError):
        render_template("{{ os.environ.SECRET }}", {"os": {"environ": {"SECRET": "x"}}})


def test_capture_path_reads_nested_json_and_rejects_unavailable_value() -> None:
    payload = {"data": {"resource": {"id": "res-7"}}}

    assert extract_json_path(payload, "$.data.resource.id") == "res-7"
    with pytest.raises(WorkflowDefinitionError):
        extract_json_path(payload, "$.data.unknown")
    with pytest.raises(WorkflowDefinitionError):
        extract_json_path(payload, "data.resource.id")


def test_invalid_json_schema_is_rejected_before_execution(tmp_path: Path) -> None:
    path = _write_definition(
        tmp_path,
        """
version: 1
name: invalid-schema
base_url: http://target
steps:
  - id: read
    name: Read
    request: {method: GET, path: /fast}
    expect:
      status: 200
      json_schema:
        type: definitely-not-a-json-schema-type
""",
    )

    with pytest.raises(WorkflowDefinitionError, match="json_schema"):
        WorkflowDefinition.from_yaml(path)


def test_capture_cannot_overwrite_reserved_template_namespaces(tmp_path: Path) -> None:
    path = _write_definition(
        tmp_path,
        """
version: 1
name: reserved-capture
base_url: http://target
steps:
  - id: create
    name: Create
    request: {method: POST, path: /resources}
    expect: {status: 201}
    capture:
      request: $.id
""",
    )

    with pytest.raises(WorkflowDefinitionError, match="reserved"):
        WorkflowDefinition.from_yaml(path)
