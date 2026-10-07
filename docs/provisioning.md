# Optional Azure resource creation

Use this only when you want a **new, dedicated demo environment**.
For already-configured resources, use the manual path in the [README](../README.md).
No resources are created by application startup.

## What is created

| Azure resource or configuration | Default |
|---|---|
| Resource group | The name you supply |
| Foundry account | `AIServices`, S0, project management enabled |
| Foundry project | `pdf-starter`, with a system-assigned identity |
| Answer-model deployment | `pdf-answer`: `gpt-4.1`, version `2025-04-14`, `GlobalStandard`, capacity 1 |
| Embedding deployment | `pdf-embedding`: `text-embedding-3-small`, version `1`, `Standard`, capacity 1, 1536-dimensional app requests |
| Azure AI Search service | Basic, one partition and one replica |
| Search index | `pdf-starter`, matching the supplied vector index schema |
| Document Intelligence account | `FormRecognizer`, S0, for `prebuilt-read` |
| Prompt-agent version | `pdf-knowledge-librarian`, strict evidence schema, no tools, using `pdf-answer` |
| RBAC | Signed-in user: Foundry User / Azure AI User and OpenAI User on Foundry, Search data/service contributor on Search, Cognitive Services User on OCR. Foundry project identity: Foundry User on the Foundry account. |

Resource names use your prefix plus a deterministic suffix based on the
subscription and resource group. Rerun with the **same arguments** to target the
same resources. Global name collisions are still possible; use a different prefix
in a separate checkout if necessary.

Both model deployments are explicitly versioned with automatic upgrades disabled.
Availability, regional capacity, supported SKUs, and subscription quota can change.
Capacity units differ by model; a value of 1 is not a general performance guarantee.
Check availability/quota for your subscription before applying.

## Permissions and prerequisites

Install the README's pinned Python dependencies and a current Azure CLI.
Authenticate with `az login` as an Entra **user**, not a service principal.
This script targets **Azure public cloud** only.

The identity needs resource-creation and role-assignment permissions: for example,
Owner, or Contributor plus User Access Administrator / Role Based Access Control
Administrator, at appropriate scopes. Creating a group and registering providers
requires subscription-level access or prior administrator setup. Policies,
regional restrictions, and model quota can still prevent deployment.

The script changes the Azure CLI's selected subscription to the one supplied.
The resulting `.env` is for that user's app; another user needs their own RBAC
assignments. The app's `DefaultAzureCredential` might select an environment
identity before CLI login; remove stale credential overrides if needed.

## Preview, then explicitly apply

From the repository root with the virtual environment active:

```bash
python -m scripts.provision_azure --subscription "<subscription-id>" --resource-group "rg-propel-pdf-demo" --location "<supported-region>" --name-prefix "propelpdf"
```

This default preview validates local settings and prints intended names,
configuration, and endpoints. It makes **no Azure calls and no file writes**.
It is not an ARM what-if or service-side quota validation.

To authorize changes, rerun with `--apply`:

```bash
python -m scripts.provision_azure --subscription "<subscription-id>" --resource-group "rg-propel-pdf-demo" --location "<supported-region>" --name-prefix "propelpdf" --apply
```

Optional model flags:

| Flag | Purpose |
|---|---|
| `--answer-model` / `--answer-model-version` | Select an available model/version supporting prompt agents, structured outputs, and `temperature=0`. |
| `--answer-sku` / `--answer-capacity` | Set the answer deployment SKU and capacity. |
| `--embedding-model-version` | Select an available `text-embedding-3-small` version. The embedding model itself remains fixed to match the app. |
| `--embedding-sku` / `--embedding-capacity` | Set the embedding deployment SKU and capacity. |

Do not change the model settings on a retry unless you intentionally want to
update those deployments. `--apply` uses an incremental ARM deployment: it does
not delete unrelated resources, but can update matching, deterministically named
resources. Use a dedicated resource group/prefix, not shared production resources.

## Configuration and retries

The script registers `Microsoft.CognitiveServices` and `Microsoft.Search`, then
deploys [`infra/azure-resources.json`](../infra/azure-resources.json).
It assigns roles using stable role IDs, waits with bounded retries for authorization
errors/throttling, creates the Search index, and creates/verifies the prompt agent.
Existing matching roles/index/agent can be reused; incompatible index or agent
configuration is reported instead of silently overwritten.

`.env` is copied from the placeholder example if absent. Resource settings are
written after ARM deployment; agent name/version are written after verification.
Unrelated entries are preserved. A concrete setting pointing to a different
resource or agent fails the local preflight **before any Azure changes**.
There are no keys, bearer tokens, or passwords in generated configuration.
Never commit `.env` or `.runtime`.

If setup fails, no automatic rollback occurs. Already-created resources remain
and may incur charges. Errors are reported; fix the problem and rerun with the
same group/prefix/model arguments. The script can recover an agent created before
the final `.env` write; multiple existing versions require a manual choice in
`.env`. RBAC propagation can exceed the five-minute retry window: wait and retry.

Common deployment failures include unregistered/unavailable models, regional
capacity, insufficient quota, policy denying public endpoints, name conflicts,
and missing role-assignment permissions. ARM reports deployment errors; inspect
the resource group's deployment history when needed.

## Costs, security, and validation limits

Basic Search generally bills while idle. Foundry/model and OCR usage can incur
additional charges. Public network access is enabled so a local workstation can
connect; key-based/local authentication is disabled on all service accounts.
This is not private networking or production hardening.

The script creates no Storage, App Service, Cosmos DB, SharePoint, or Fabric
resources. It does not delete resources or perform cleanup. An administrator
should manage costs and eventual resource retirement separately.

Local tests cover the template structure, CLI orchestration, preview safety,
configuration preservation, and retry/reuse behavior using doubles.
**The provisioning script has not been run against a live Azure subscription.**
Resource/model availability and live provisioning still require verification.
After setup, run the app and use the sample questions to check your environment.
