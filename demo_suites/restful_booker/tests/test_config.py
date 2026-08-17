from pathlib import Path

from utils.config_loader import load_yaml, resolve_environment_variables


def test_load_yaml_returns_mapping_for_environment_config() -> None:
    config_path = Path(__file__).parents[1] / "config" / "config.yaml"

    config = load_yaml(config_path)

    assert config["base_url"].startswith("https://")
    assert config["auth"]["username"]


def test_resolve_environment_variables_uses_default_and_override(monkeypatch) -> None:
    config = {"auth": {"username": "${BOOKER_USER:-admin}", "password": "${BOOKER_PASSWORD:-fallback}"}}

    monkeypatch.setenv("BOOKER_PASSWORD", "from-environment")

    resolved = resolve_environment_variables(config)

    assert resolved == {"auth": {"username": "admin", "password": "from-environment"}}


def test_resolve_environment_variables_uses_default_for_empty_value(monkeypatch) -> None:
    monkeypatch.setenv("BOOKER_USER", "")

    resolved = resolve_environment_variables("${BOOKER_USER:-admin}")

    assert resolved == "admin"


def test_standalone_pytest_configuration_enables_strict_markers() -> None:
    pytest_ini = (Path(__file__).parents[1] / "pytest.ini").read_text(encoding="utf-8")

    assert "--strict-markers" in pytest_ini
    assert "api: tests that call the public Restful Booker service" in pytest_ini
    assert "smoke: core end-to-end API regression tests" in pytest_ini
