"""Explicit administrator setup; never called by the running application."""
import argparse
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.identity import AzureCliCredential
from dotenv import load_dotenv

from .azure import required
from .core import Selection

ROOT = Path(__file__).resolve().parents[1]


def agent_definition(model: str) -> PromptAgentDefinition:
    return PromptAgentDefinition({
        "kind": "prompt",
        "model": model,
        "instructions": (ROOT / "agent-instructions.txt").read_text(encoding="utf-8"),
        "tools": [],
        "tool_choice": "none",
        "temperature": 0,
        "text": {"format": {
            "type": "json_schema",
            "name": "grounded_selection",
            "strict": True,
            "schema": Selection.model_json_schema(),
        }},
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a dedicated PDF prompt agent version.")
    parser.add_argument("--name", required=True, help="New, dedicated agent name.")
    parser.add_argument("--model", required=True, help="Existing answer-model deployment name.")
    parser.add_argument("--new-version", action="store_true", help="Allow versioning an existing agent.")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    definition = agent_definition(args.model)
    with AzureCliCredential() as credential, AIProjectClient(
        endpoint=required("FOUNDRY_PROJECT_ENDPOINT"), credential=credential,
    ) as project:
        if not args.new_version and any(agent.name == args.name for agent in project.agents.list()):
            parser.error("Agent already exists. Choose a new name or explicitly pass --new-version.")
        created = project.agents.create_version(
            agent_name=args.name,
            definition=definition,
            description="Tool-free, grounded PDF evidence selection for the local starter kit.",
        )
        saved = project.agents.get_version(agent_name=created.name, agent_version=created.version)
        actual = saved.definition.as_dict()
        for key, expected in definition.as_dict().items():
            if actual.get(key) != expected:
                raise RuntimeError(f"Saved agent configuration does not match requested {key}.")
        print(f"FOUNDRY_AGENT_NAME={saved.name}")
        print(f"FOUNDRY_AGENT_VERSION={saved.version}")


if __name__ == "__main__":
    main()
