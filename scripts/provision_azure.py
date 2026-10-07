"""Opt-in Azure setup for a dedicated demo; never invoked by app startup."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity import AzureCliCredential
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import SearchIndex
from dotenv import dotenv_values, set_key
from openai import APIStatusError

from app.setup_agent import agent_definition, create_verified_version

ROOT = Path(__file__).resolve().parents[1]
# Stable role IDs avoid display-name changes (Azure AI User / Foundry User).
FOUNDRY_USER = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
OPENAI_USER = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
SEARCH_DATA = "8ebe5a00-799e-43f5-93ac-243d3dce84a7"
SEARCH_SERVICE = "7ca78c08-252a-4471-8644-bb5ff32d4ba0"
COGNITIVE_USER = "a97b65f3-24c7-4388-baec-2e87135dc908"
AGENT_NAME = "pdf-knowledge-librarian"
INDEX_NAME = "pdf-starter"


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True, help="Azure public-cloud subscription UUID.")
    parser.add_argument("--resource-group", required=True, help="Dedicated demo resource group.")
    parser.add_argument("--location", required=True, help="Region supporting both models and OCR.")
    parser.add_argument("--name-prefix", default="propelpdf", help="3-16 lowercase letters/digits, starting with a letter.")
    parser.add_argument("--answer-model", default="gpt-4.1")
    parser.add_argument("--answer-model-version", default="2025-04-14")
    parser.add_argument("--answer-sku", default="GlobalStandard")
    parser.add_argument("--answer-capacity", type=int, default=1)
    parser.add_argument("--embedding-model-version", default="1")
    parser.add_argument("--embedding-sku", default="Standard")
    parser.add_argument("--embedding-capacity", type=int, default=1)
    parser.add_argument("--apply", action="store_true", help="Explicitly authorize paid resources, roles, index, agent, and .env writes.")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z][a-z0-9]{2,15}", args.name_prefix):
        parser.error("--name-prefix must be 3-16 lowercase letters/digits starting with a letter.")
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", args.subscription):
        parser.error("--subscription must be a subscription UUID.")
    if args.answer_capacity < 1 or args.embedding_capacity < 1:
        parser.error("Model capacities must be positive.")
    return args


def configuration(args):
    suffix = hashlib.sha256(
        f"{args.subscription.lower()}/{args.resource_group.lower()}/{args.name_prefix}".encode()
    ).hexdigest()[:8]
    base = f"{args.name_prefix}-{suffix}"
    parameters = {
        "location": args.location,
        "foundryName": f"{base}-ai",
        "searchName": f"{base}-search",
        "ocrName": f"{base}-ocr",
        "projectName": "pdf-starter",
        "answerModel": args.answer_model,
        "answerModelVersion": args.answer_model_version,
        "answerSku": args.answer_sku,
        "answerCapacity": args.answer_capacity,
        "embeddingModelVersion": args.embedding_model_version,
        "embeddingSku": args.embedding_sku,
        "embeddingCapacity": args.embedding_capacity,
    }
    settings = {
        "FOUNDRY_PROJECT_ENDPOINT": f"https://{base}-ai.services.ai.azure.com/api/projects/pdf-starter",
        "FOUNDRY_EMBEDDING_MODEL": "pdf-embedding",
        "FOUNDRY_EMBEDDING_ENDPOINT": f"https://{base}-ai.openai.azure.com",
        "SEARCH_ENDPOINT": f"https://{base}-search.search.windows.net",
        "SEARCH_INDEX_NAME": INDEX_NAME,
        "DOCUMENT_INTELLIGENCE_ENDPOINT": f"https://{base}-ocr.cognitiveservices.azure.com/",
    }
    return parameters, settings


def env_preflight(path, settings):
    current = dotenv_values(path) if path.exists() else {}
    for key, expected in settings.items():
        value = current.get(key)
        if value and "<" not in value and value.rstrip("/") != expected.rstrip("/"):
            raise RuntimeError(
                f"{path.name} already points to different resources ({key}). "
                "Use the manual setup for existing resources, or a separate checkout for new resources."
            )
    name, version = current.get("FOUNDRY_AGENT_NAME"), current.get("FOUNDRY_AGENT_VERSION")
    concrete_name = bool(name and "<" not in name)
    concrete_version = bool(version and "<" not in version)
    if concrete_name != concrete_version or (concrete_name and name != AGENT_NAME):
        raise RuntimeError("Existing agent settings conflict with this script's dedicated agent; use manual setup.")
    return version if concrete_version else None


def write_env(path, settings):
    if not path.exists():
        path.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    for key, value in settings.items():
        set_key(str(path), key, str(value), quote_mode="always")


def az(*args):
    executable = shutil.which("az")
    if not executable:
        raise RuntimeError("Install Azure CLI and run az login before using --apply.")
    result = subprocess.run(
        [executable, *args, "--only-show-errors", "--output", "json"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Azure CLI failed ({' '.join(args[:3])}):\n{result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def grant(subscription, principal_id, principal_type, role, scope):
    existing = az(
        "role", "assignment", "list", "--subscription", subscription,
        "--scope", scope, "--all",
    )
    if any(
        row["principalId"].lower() == principal_id.lower()
        and row["roleDefinitionId"].split("/")[-1].lower() == role
        and row["scope"].lower() == scope.lower()
        for row in existing
    ):
        return
    az(
        "role", "assignment", "create", "--subscription", subscription,
        "--assignee-object-id", principal_id, "--assignee-principal-type", principal_type,
        "--role", role, "--scope", scope,
    )


def wait_for_access(operation, description, timeout=300):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return operation()
        except (HttpResponseError, APIStatusError) as error:
            if error.status_code not in {401, 403, 429} or time.monotonic() >= deadline:
                raise
            print(f"Waiting for Azure access: {description} ({error.status_code}); retrying in 15 seconds.", flush=True)
            time.sleep(15)


def ensure_index(client):
    desired = json.loads((ROOT / "search-index.json").read_text(encoding="utf-8"))
    desired["name"] = INDEX_NAME
    try:
        actual = client.get_index(INDEX_NAME)
    except ResourceNotFoundError:
        client.create_index(SearchIndex.from_dict(desired))
        return
    expected = SearchIndex.from_dict(desired).as_dict()
    existing = actual.as_dict()
    # Server responses include defaults. Compare every explicitly specified property.
    def contains(actual_value, expected_value):
        if isinstance(expected_value, dict):
            return isinstance(actual_value, dict) and all(
                key in actual_value and contains(actual_value[key], value)
                for key, value in expected_value.items()
            )
        if isinstance(expected_value, list):
            return isinstance(actual_value, list) and len(actual_value) == len(expected_value) and all(
                contains(actual_item, expected_item)
                for actual_item, expected_item in zip(actual_value, expected_value, strict=True)
            )
        return actual_value == expected_value

    if not contains(existing, expected):
        raise RuntimeError("Existing index differs from search-index.json; it was not changed. Use a fresh dedicated setup.")


def ensure_agent(project, model, version):
    if version:
        saved = project.agents.get_version(agent_name=AGENT_NAME, agent_version=version)
    else:
        # A previous attempt may have created an agent before .env was saved.
        exists = any(agent.name == AGENT_NAME for agent in project.agents.list())
        existing = list(project.agents.list_versions(agent_name=AGENT_NAME)) if exists else []
        if existing:
            if len(existing) != 1:
                raise RuntimeError("Multiple agent versions exist; select the intended version manually in .env.")
            saved = project.agents.get_version(agent_name=AGENT_NAME, agent_version=existing[0].version)
        else:
            return create_verified_version(project, AGENT_NAME, model)
    actual = saved.definition.as_dict()
    if any(actual.get(key) != value for key, value in agent_definition(model).as_dict().items()):
        raise RuntimeError("Existing agent differs from this sample's configuration; it was not changed.")
    return saved


def provision(args):
    parameters, settings = configuration(args)
    env_path = ROOT / ".env"
    agent_version = env_preflight(env_path, settings)
    print(json.dumps({
        "subscription": args.subscription,
        "resource_group": args.resource_group,
        "resources_and_models": parameters,
        "env_settings": settings,
        "agent": AGENT_NAME,
    }, indent=2))
    print("Paid Basic Search and S0 AI/OCR resources; public endpoints with Entra-only access.")
    if not args.apply:
        print("Preview only: nothing created or written. Add --apply to authorize setup.")
        return
    if az("cloud", "show")["name"] != "AzureCloud":
        raise RuntimeError("This template targets Azure public cloud only.")
    az("account", "set", "--subscription", args.subscription)
    account = az("account", "show", "--subscription", args.subscription)
    if account["user"]["type"] != "user":
        raise RuntimeError("Run az login as a user; this setup grants roles to the signed-in user.")
    principal = az("ad", "signed-in-user", "show")["id"]
    for provider in ["Microsoft.CognitiveServices", "Microsoft.Search"]:
        az("provider", "register", "--subscription", args.subscription, "--namespace", provider, "--wait")
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    parameter_path = runtime / "azure-parameters.json"
    parameter_path.write_text(json.dumps({
        "parameters": {key: {"value": value} for key, value in parameters.items()},
    }), encoding="utf-8")
    az(
        "group", "create", "--subscription", args.subscription,
        "--name", args.resource_group, "--location", args.location,
    )
    print("Deploying Azure resources and models. This can take several minutes.", flush=True)
    deployment = az(
        "deployment", "group", "create", "--subscription", args.subscription,
        "--resource-group", args.resource_group, "--name", "pdf-starter",
        "--mode", "Incremental", "--template-file", str(ROOT / "infra" / "azure-resources.json"),
        "--parameters", f"@{parameter_path}",
    )
    outputs = {key: value["value"] for key, value in deployment["properties"]["outputs"].items()}
    write_env(env_path, settings)
    for scope_key, role in [
        ("foundryResourceId", FOUNDRY_USER),
        ("foundryResourceId", OPENAI_USER),
        ("searchResourceId", SEARCH_DATA),
        ("searchResourceId", SEARCH_SERVICE),
        ("ocrResourceId", COGNITIVE_USER),
    ]:
        grant(args.subscription, principal, "User", role, outputs[scope_key])
    grant(args.subscription, outputs["projectPrincipalId"], "ServicePrincipal", FOUNDRY_USER, outputs["foundryResourceId"])
    with AzureCliCredential(tenant_id=account["tenantId"]) as credential:
        with SearchIndexClient(settings["SEARCH_ENDPOINT"], credential) as client:
            wait_for_access(lambda: ensure_index(client), "Search index")
        with AIProjectClient(endpoint=settings["FOUNDRY_PROJECT_ENDPOINT"], credential=credential) as project:
            saved = wait_for_access(
                lambda: ensure_agent(project, "pdf-answer", agent_version), "Foundry agent",
            )
    write_env(env_path, {"FOUNDRY_AGENT_NAME": saved.name, "FOUNDRY_AGENT_VERSION": str(saved.version)})
    print("Setup complete; .env populated. Run the app and verify the sample questions.")


def main():
    try:
        provision(arguments())
    except (RuntimeError, OSError, ValueError, HttpResponseError, APIStatusError) as error:
        raise SystemExit(
            f"Setup failed: {error}\n"
            "Already-created resources remain and may incur charges. No resources were deleted. "
            "Resolve the error and rerun with the same subscription, group, prefix, and model settings."
        ) from error


if __name__ == "__main__":
    main()
