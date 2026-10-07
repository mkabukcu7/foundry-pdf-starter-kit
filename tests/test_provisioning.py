import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.search.documents.indexes.models import SearchIndex
from dotenv import dotenv_values

from scripts import provision_azure as setup

SUBSCRIPTION = "00000000-0000-0000-0000-000000000001"
BASE_ARGS = [
    "--subscription", SUBSCRIPTION, "--resource-group", "rg-demo",
    "--location", "eastus", "--name-prefix", "propelpdf",
]


def test_configuration_is_deterministic_and_contains_all_resource_settings():
    first = setup.configuration(setup.arguments(BASE_ARGS))
    assert first == setup.configuration(setup.arguments(BASE_ARGS))
    parameters, env = first
    assert set(env) == {
        "FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_EMBEDDING_MODEL",
        "FOUNDRY_EMBEDDING_ENDPOINT", "SEARCH_ENDPOINT", "SEARCH_INDEX_NAME",
        "DOCUMENT_INTELLIGENCE_ENDPOINT",
    }
    assert parameters["answerModel"] == "gpt-4.1"
    assert parameters["answerModelVersion"] == "2025-04-14"
    assert env["FOUNDRY_EMBEDDING_MODEL"] == "pdf-embedding"
    assert env["SEARCH_INDEX_NAME"] == "pdf-starter"
    assert setup.configuration(setup.arguments([
        *BASE_ARGS, "--resource-group", "another-group",
    ])) != first


@pytest.mark.parametrize("extra", [
    ["--name-prefix", "Bad Prefix"],
    ["--subscription", "not-an-id"],
    ["--answer-capacity", "0"],
    ["--embedding-capacity", "-1"],
])
def test_invalid_arguments_rejected(extra):
    with pytest.raises(SystemExit):
        setup.arguments([*BASE_ARGS, *extra])


def test_preview_makes_no_cloud_calls_or_file_writes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    cli = Mock(side_effect=AssertionError("Preview must not call Azure"))
    monkeypatch.setattr(setup, "az", cli)
    setup.provision(setup.arguments(BASE_ARGS))
    cli.assert_not_called()
    assert not list(tmp_path.iterdir())
    assert "Preview only" in capsys.readouterr().out


def test_env_creation_populates_placeholders_without_keys(tmp_path):
    env = tmp_path / ".env"
    _, settings = setup.configuration(setup.arguments(BASE_ARGS))
    assert setup.env_preflight(env, settings) is None
    setup.write_env(env, settings)
    values = dotenv_values(env)
    assert all(values[key] == value for key, value in settings.items())
    assert "<" in values["FOUNDRY_AGENT_VERSION"]
    assert not any("KEY" in key or "TOKEN" in key or "SECRET" in key for key in values)
    setup.write_env(env, {
        "FOUNDRY_AGENT_NAME": setup.AGENT_NAME, "FOUNDRY_AGENT_VERSION": "1",
    })
    assert setup.env_preflight(env, settings) == "1"


def test_env_updates_preserve_unrelated_entries(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# Local preferences\nLOCAL_NOTE=keep\nSEARCH_ENDPOINT=<placeholder>\n")
    setup.write_env(env, {"SEARCH_ENDPOINT": "https://new.search.windows.net"})
    assert dotenv_values(env)["LOCAL_NOTE"] == "keep"
    assert "# Local preferences" in env.read_text()


@pytest.mark.parametrize("entry", [
    "SEARCH_ENDPOINT=https://existing.search.windows.net\n",
    "FOUNDRY_AGENT_NAME=another-agent\nFOUNDRY_AGENT_VERSION=1\n",
    "FOUNDRY_AGENT_NAME=pdf-knowledge-librarian\nFOUNDRY_AGENT_VERSION=<version>\n",
])
def test_existing_conflicting_env_fails_before_azure(tmp_path, monkeypatch, entry):
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    env = tmp_path / ".env"
    env.write_text(entry)
    cli = Mock()
    monkeypatch.setattr(setup, "az", cli)
    with pytest.raises(RuntimeError):
        setup.provision(setup.arguments([*BASE_ARGS, "--apply"]))
    cli.assert_not_called()
    assert env.read_text() == entry


def test_role_reuse_and_creation(monkeypatch):
    scope = "/subscriptions/sub/resourceGroups/demo/providers/Microsoft.Search/searchServices/search"
    principal = "user-id"
    cli = Mock(return_value=[{
        "principalId": principal,
        "roleDefinitionId": f"/providers/Microsoft.Authorization/roleDefinitions/{setup.SEARCH_DATA}",
        "scope": scope,
    }])
    monkeypatch.setattr(setup, "az", cli)
    setup.grant(SUBSCRIPTION, principal, "User", setup.SEARCH_DATA, scope)
    assert cli.call_count == 1
    cli.reset_mock()
    cli.return_value = []
    setup.grant(SUBSCRIPTION, principal, "User", setup.SEARCH_DATA, scope)
    assert cli.call_count == 2
    assert cli.call_args.args[:3] == ("role", "assignment", "create")


def test_ensure_index_creates_missing_without_update():
    client = Mock()
    client.get_index.side_effect = ResourceNotFoundError("missing")
    setup.ensure_index(client)
    index = client.create_index.call_args.args[0]
    assert index.name == setup.INDEX_NAME
    assert {f.name: f for f in index.fields}["vector"].vector_search_dimensions == 1536
    client.create_or_update_index.assert_not_called()


def test_ensure_index_reuses_matching_and_refuses_incompatible():
    raw = json.loads((setup.ROOT / "search-index.json").read_text())
    raw["name"] = setup.INDEX_NAME
    client = Mock()
    client.get_index.return_value = SearchIndex.from_dict(raw)
    setup.ensure_index(client)
    client.create_index.assert_not_called()
    raw["fields"][-1]["dimensions"] = 100
    client.get_index.return_value = SearchIndex.from_dict(raw)
    with pytest.raises(RuntimeError, match="not changed"):
        setup.ensure_index(client)
    client.create_or_update_index.assert_not_called()


def test_ensure_agent_creates_and_verifies_missing():
    project = Mock()
    project.agents.list.return_value = []
    saved = SimpleNamespace(name=setup.AGENT_NAME, version="1", definition=setup.agent_definition("pdf-answer"))
    project.agents.create_version.return_value = saved
    project.agents.get_version.return_value = saved
    assert setup.ensure_agent(project, "pdf-answer", None) == saved
    project.agents.create_version.assert_called_once()
    project.agents.list_versions.assert_not_called()


@pytest.mark.parametrize("version", [None, "1"])
def test_ensure_agent_reuses_verified_version(version):
    project = Mock()
    saved = SimpleNamespace(name=setup.AGENT_NAME, version="1", definition=setup.agent_definition("pdf-answer"))
    project.agents.list.return_value = [SimpleNamespace(name=setup.AGENT_NAME)]
    project.agents.list_versions.return_value = [saved]
    project.agents.get_version.return_value = saved
    assert setup.ensure_agent(project, "pdf-answer", version) == saved
    project.agents.create_version.assert_not_called()


def test_incompatible_or_ambiguous_agent_is_not_changed():
    project = Mock()
    project.agents.get_version.return_value = SimpleNamespace(definition=setup.agent_definition("wrong-model"))
    with pytest.raises(RuntimeError, match="not changed"):
        setup.ensure_agent(project, "pdf-answer", "1")
    project.agents.list.return_value = [SimpleNamespace(name=setup.AGENT_NAME)]
    project.agents.list_versions.return_value = [SimpleNamespace(version="1"), SimpleNamespace(version="2")]
    with pytest.raises(RuntimeError, match="Multiple"):
        setup.ensure_agent(project, "pdf-answer", None)
    project.agents.create_version.assert_not_called()


def status_error(status):
    error = HttpResponseError("synthetic")
    error.status_code = status
    return error


def test_access_retries_are_bounded_and_explicit(monkeypatch, capsys):
    clock = Mock(side_effect=[0, 0, 301])
    sleep = Mock()
    monkeypatch.setattr(setup.time, "monotonic", clock)
    monkeypatch.setattr(setup.time, "sleep", sleep)
    operation = Mock(side_effect=status_error(403))
    with pytest.raises(HttpResponseError):
        setup.wait_for_access(operation, "test")
    assert operation.call_count == 2
    sleep.assert_called_once_with(15)
    assert "Waiting for Azure access" in capsys.readouterr().out


def test_access_does_not_retry_other_service_failures():
    operation = Mock(side_effect=status_error(400))
    with pytest.raises(HttpResponseError):
        setup.wait_for_access(operation, "test")
    operation.assert_called_once()


def test_azure_cli_failure_is_explicit_and_arguments_are_not_shell(monkeypatch):
    monkeypatch.setattr(setup.shutil, "which", lambda name: "/bin/az")
    runner = Mock(return_value=SimpleNamespace(returncode=1, stdout="", stderr="Permission denied"))
    monkeypatch.setattr(setup.subprocess, "run", runner)
    with pytest.raises(RuntimeError, match="Permission denied"):
        setup.az("group", "create", "--name", "demo")
    assert isinstance(runner.call_args.args[0], list)
    assert "shell" not in runner.call_args.kwargs


def test_template_lists_needed_services_and_disables_keys():
    template = json.loads((setup.ROOT / "infra/azure-resources.json").read_text())
    resources = template["resources"]
    assert {resource["type"] for resource in resources} == {
        "Microsoft.CognitiveServices/accounts",
        "Microsoft.CognitiveServices/accounts/projects",
        "Microsoft.CognitiveServices/accounts/deployments",
        "Microsoft.Search/searchServices",
    }
    accounts = [r for r in resources if r["type"] == "Microsoft.CognitiveServices/accounts"]
    assert {r["kind"] for r in accounts} == {"AIServices", "FormRecognizer"}
    assert all(r["properties"]["disableLocalAuth"] for r in accounts)
    search = next(r for r in resources if r["type"] == "Microsoft.Search/searchServices")
    assert search["sku"]["name"] == "basic"
    assert search["properties"]["disableLocalAuth"]
    parameters, _ = setup.configuration(setup.arguments(BASE_ARGS))
    assert set(parameters) == set(template["parameters"])


def test_apply_orchestration_populates_all_eight_settings(tmp_path, monkeypatch):
    original_root = setup.ROOT
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    (tmp_path / ".env.example").write_text((original_root / ".env.example").read_text())
    calls = []
    outputs = {key: {"value": key} for key in [
        "foundryResourceId", "projectResourceId", "projectPrincipalId",
        "searchResourceId", "ocrResourceId",
    ]}

    def cli(*args):
        calls.append(args)
        if args[:2] == ("cloud", "show"):
            return {"name": "AzureCloud"}
        if args[:2] == ("account", "show"):
            return {"tenantId": "tenant", "user": {"type": "user"}}
        if args[:3] == ("ad", "signed-in-user", "show"):
            return {"id": "principal"}
        if args[:3] == ("deployment", "group", "create"):
            return {"properties": {"outputs": outputs}}
        return None

    monkeypatch.setattr(setup, "az", cli)
    grant = Mock()
    monkeypatch.setattr(setup, "grant", grant)
    credential = Mock()
    credential.__enter__ = Mock(return_value=credential)
    credential.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(setup, "AzureCliCredential", Mock(return_value=credential))
    index_client, project = Mock(), Mock()
    for client in [index_client, project]:
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(setup, "SearchIndexClient", Mock(return_value=index_client))
    monkeypatch.setattr(setup, "AIProjectClient", Mock(return_value=project))
    ensure_index = Mock()
    monkeypatch.setattr(setup, "ensure_index", ensure_index)
    monkeypatch.setattr(setup, "ensure_agent", Mock(return_value=SimpleNamespace(name=setup.AGENT_NAME, version="1")))
    setup.provision(setup.arguments([*BASE_ARGS, "--apply"]))
    values = dotenv_values(tmp_path / ".env")
    assert len(values) == 8
    assert all(value and "<" not in value for value in values.values())
    assert grant.call_count == 6
    ensure_index.assert_called_once_with(index_client)
    assert ("account", "set", "--subscription", SUBSCRIPTION) in calls
    assert sum(call[:2] == ("provider", "register") for call in calls) == 2
    assert not any("delete" in call for call in calls)
    # Same run parameters are persisted without credentials.
    persisted = json.loads((tmp_path / ".runtime" / "azure-parameters.json").read_text())
    assert persisted["parameters"]["answerModel"]["value"] == "gpt-4.1"
