# Propel Microsoft Foundry PDF starter kit

A consumable, standalone hello-world for the Propel Enablement team: upload one
text-based PDF, retrieve passages from Azure AI Search, and ask one Microsoft
Foundry Knowledge Librarian agent to select grounded evidence with citations.
This repository is self-contained and does not import the original demo.

**Intentionally small:** Python + FastAPI + one HTML page; one local user, one
current document, one process. No SharePoint, classification, taxonomy, approvals,
write-back, MCP, Fabric, frontend build system, agent tools, or chat history.
Scanned PDFs requiring OCR are outside scope. Blank/image-only pages are skipped;
mixed PDFs expose only their extractable text, not their images or scanned pages.

## What was reused

Originally extracted from [mkabukcu7/sharepoint-foundry](https://github.com/mkabukcu7/sharepoint-foundry).
The paths below describe that original repository, not dependencies of this sample.

The parent `backend/app/services/search.py` provides the adapted per-page
1200-character/150-character-overlap chunking, hybrid `VectorizedQuery`, Entra
authentication, and per-record indexing-result checks. The parent
`knowledge_librarian.py` provides the `AIProjectClient.get_openai_client()` +
Responses API `agent_reference` invocation pattern. The librarian prompt and
`chat_guardrails.py` inform the untrusted-data and insufficient-evidence rules;
`librarian_chat.py` informs the local-only boundary. Its simple regex PDF extractor
is replaced with `pypdf` to preserve real, one-based physical page numbers.

Embeddings retain the parent Search service's `AzureOpenAI` deployment-endpoint
pattern with Entra tokens and API version `2024-10-21`; agent calls use the
separate Foundry project endpoint. Both deployments belong to your Foundry resource.

The dependency set is pinned, including the OpenAI client. Local compatibility
tests exercise the installed Foundry client's Entra-backed OpenAI factory,
Responses request serialization, Search index deserialization and vector query
parameters. This is **not** proof of live service compatibility: model availability,
RBAC, endpoint behavior, structured output support, and agent configuration still
require the live checks below.

## Prerequisites and manual Azure setup

Use Python **3.12**, Azure CLI, an Azure subscription, and permission to have an
administrator configure resources/role assignments. This app never provisions,
deploys, updates or deletes Azure resources. It only writes/deletes **document
records in your dedicated Search index**, as required for PDF replacement.

| Azure-hosted component | Manual setup |
|---|---|
| Microsoft Foundry resource and project | Copy the project endpoint in the form `https://<resource>.services.ai.azure.com/api/projects/<project>`. Use a current Foundry project supporting versioned prompt agents and the Responses API, not a classic hub-only endpoint. |
| Answer model deployment | Deploy a model supporting structured JSON-schema outputs and prompt agents, for example `gpt-4.1-mini` if offered in your region. This deployment is chosen on the agent, not by the local code. |
| Embedding deployment | Deploy `text-embedding-3-small`. Set its **deployment name**, not necessarily its model name, and its resource's Azure OpenAI endpoint (`https://<resource>.openai.azure.com`) in `.env`. Copy that endpoint from the deployment's connection details, not the project URL. This sample fixes dimensions at **1536**. |
| One prompt agent | In Foundry, create a prompt agent using the answer deployment, paste `agent-instructions.txt` as its instructions, configure **no tools**, and publish/save a version. Copy its name and version into `.env`. The app also supplies these instructions on each request and disables tool use. Do not point at the existing enterprise librarian. |
| Azure AI Search service and index | Enable role-based data access (RBAC) and vector search. Create a **new dedicated index** using `search-index.json`, replacing its `name` placeholder. A vector-capable Basic or higher service is a straightforward choice; check regional/tier availability and limits. No semantic ranker, indexer, skillset, blob storage, or Search-to-Foundry connection is needed. |

An administrator can create the index with the Azure portal's index JSON editor,
or use the Search data-plane REST API `PUT /indexes/<index>?api-version=2024-07-01`
with the JSON file and an Entra bearer token for `https://search.azure.com`.
Do this manually; the application calls only `get_index` to check the schema.
Do not reuse or modify the parent demo's index.

Assign roles to the **identity that runs the local Python process**:

| Scope | Role and purpose |
|---|---|
| Foundry project | **Azure AI User** for invoking the configured agent and models. Verify its inherited model-deployment access; where required by your resource's RBAC configuration, grant **Cognitive Services OpenAI User** on the Foundry resource for model/embedding inference. Agent/model creation needs separate administrator/developer rights during manual setup. |
| Search service | **Search Index Data Contributor** for querying, uploading, and deleting document records. |
| Search service | **Search Service Contributor** for reading index definitions during startup validation. This built-in role has broader schema-management rights than the app uses; use a custom least-privilege schema-read role if desired. |

The Foundry agent itself does not query Search. The local backend retrieves and
passes evidence, so its Entra identity needs the above access; no Search role
assignment to an agent identity is necessary for this flow.
Allow your workstation through service firewalls/private networking. Role
assignments can take several minutes to propagate.

**Costs:** Search generally incurs ongoing service charges even while idle.
Embeddings and agent calls incur model usage charges; additional agent/platform
charges may apply. Check current regional pricing and quotas before manual setup.
Each upload embeds its text; each question embeds the question and makes at most
one agent call. Tests use doubles and incur no Azure charges. The app does not
delete resources or automatically clean the index on shutdown.

## Setup and run

From this directory:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

On Windows PowerShell use `.\.venv\Scripts\Activate.ps1` and
`Copy-Item .env.example .env` instead. Fill the seven placeholders in `.env` with
resource endpoints, deployment name, agent name/version and dedicated index name.
There are no API keys or secrets in this configuration.

```bash
az login
az account set --subscription "<your-subscription-id>"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Open **http://127.0.0.1:8000**. `DefaultAzureCredential` uses your Azure CLI login
locally (or another supported Entra identity, such as managed identity in an
adapted environment). It does **not** sign browser users in. Do not expose this
sample on a network, run multiple workers/replicas, trust proxy forwarding, or
use it as a multi-user authorization example.

## Ingest and ask

Upload `samples/moonflower.pdf` (synthetic, two pages), then try:

1. How long does the Moonflower workshop last? Expected: 45 minutes, page 1.
2. Who owns the workshop, and how long is the upload-and-questions activity?
   Expected: Propel Enablement team and 20 minutes, page 2.
3. What is the workshop's catering budget? Deliberately unsupported.

`samples/questions.txt` includes exact expected evidence. Regenerate the PDF with
`python samples/make_sample.py`; no PDF-generation dependency is added.

Ingestion validates extension, MIME type, PDF signature, encryption, size (5 MiB),
pages (50), and text/chunk count (500). It extracts and normalizes text per page,
chunks **within** pages, embeds batches of 16, and uploads text, vectors, document
UUID/name, page number, and chunk ID. Page numbers are physical PDF pages, not
printed page labels. Layout, tables, complex reading order and images are not
interpreted.

Questions use hybrid keyword/vector retrieval, with `document_id` **pre-filtering**
and top 5 results. The application checks the returned document identity again.
The agent selects up to three verbatim evidence quotes or explicitly abstains.
The backend verifies every selected chunk ID and exact quote, then builds each
citation from indexed metadata, never model-invented filenames/pages.

**Answers are deliberately extractive**, not free-form summaries: the quoted
evidence is the answer. This avoids unsupported generated prose in a foundational
sample. If no hits exist, or the agent says the passages cannot answer the question,
the app displays: **"The document does not support an answer to this question."**
An invalid model response is an explicit service error, not an unsupported answer.

### Replacement and local state

Every valid replacement gets a new UUID, including a same-name PDF. A process lock
serializes upload and Q&A; after successful replacement, old tab UUIDs receive HTTP
409. Old conversations are not sent to the model.

`.runtime/state.json` is an ignored, atomic local ledger of the active document and
known Search chunk keys; raw PDFs and extracted text are not stored locally.
Before replacing, the backend invalidates the active document and persists that
state, deletes **all known old chunk keys**, then records the new keys **before**
uploading. Only fully acknowledged indexing activates the new PDF. Search deletion
and indexing are eventually consistent: unique UUID filtering prevents stale
records from becoming evidence. A failed delete/index operation disables Q&A and
retains keys for cleanup on the next upload, including after a restart. An invalid
PDF is rejected before this transition and leaves the current PDF unchanged.

Preserve the ledger and use exactly one copy of this app per dedicated index.
Do not remove `.runtime` to resolve an error: doing so loses the record-cleanup
ledger. If it is lost, have an administrator reconcile orphaned records manually
before reusing the index. Content may remain indexed after shutdown; replacement
removes known records, not the Azure Search resource.

## Architecture and foundational concepts

The editable Mermaid source is **`architecture.mmd`**. Its local and Azure
subgraphs distinguish what runs on the workstation from hosted services.

| Concept | In this sample |
|---|---|
| Agent | One versioned Foundry prompt agent: a model plus instructions selecting evidence, without tools or autonomous workflows. |
| Grounding | Answer context comes only from retrieved passages of the current PDF, not general knowledge or previous turns. |
| Chunking | Split each page into 1200-character passages with 150-character overlap so evidence retains its source page. |
| Embeddings | A 1536-number representation of passage/question meaning generated by the Foundry embedding deployment. |
| Retrieval | Azure AI Search combines keyword and vector results; a UUID filter restricts search to the current upload. |
| Citations | Backend-generated document name and one-based page number tied to validated verbatim evidence. |
| Authentication | `DefaultAzureCredential` obtains Entra access tokens for Azure services; Azure RBAC authorizes operations. Browser access is loopback-only, not Entra user authentication. |

PDF contents, filenames and questions are serialized as **untrusted data**.
Agent instructions forbid following embedded instructions, tool use is disabled,
no history is retained, and the UI renders output as text, not HTML. These are
defense-in-depth controls, **not a guarantee that a model cannot be manipulated**.
The model still judges whether quotes actually answer a question. A relevant-topic
passage can be insufficient; vector similarity alone is not proof of support.
Review citations. Evaluate your real documents and adversarial questions before
extending this example. Top-5 retrieval can miss evidence; an unsupported response
means the retrieved passages did not support the answer, not exhaustive proof
that the entire PDF lacks it.

## Local tests and live checks

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -c pytest.ini --confcutdir=tests tests -q
python -m pip check
```

`--confcutdir=tests` isolates this standalone suite from the parent demo's
`conftest.py`. Tests cover scoping, same-name replacement, citations/pages,
unsupported questions, malformed/oversized/encrypted/scanned uploads, partial
index/delete failures, restart recovery, local-only HTTP access, and installed
SDK wire shapes. They use synthetic PDFs, an in-memory Search double and a
mock HTTP transport; they do **not** establish live model abstention or resistance
to prompt injection.

With your Azure resources configured, manually verify all three sample questions,
then replace the PDF with a different text PDF containing a different fact.
Confirm citations use the replacement name/pages, old questions cannot use the
old fact, and an old tab receives 409. Try a PDF containing "ignore prior
instructions" and verify it does not override the evidence-selection rules.
Search may take seconds to make an acknowledged upload queryable; retry a
question if immediate retrieval returns no passages.

No live Azure calls were made while building this kit. Use the handoff's local
test results as offline evidence only; the above live checks remain necessary.

Local build verification: **37 starter-kit tests and 207 existing demo tests
passed**, with no failures. `pip check` reported no dependency conflicts. Both
suites emitted one upstream Starlette/AnyIO deprecation warning. These results
are not live Azure end-to-end results.

## Troubleshooting

| Symptom | Check |
|---|---|
| Startup says a setting is missing | Replace all `.env` placeholders; start from this directory. Environment variables take precedence over `.env`. |
| Credential failure / 401 | Run `az login` in the correct tenant. Check which credential `DefaultAzureCredential` selected; stale environment/service-principal settings can take precedence over CLI login. |
| 403 from Azure | Check role scope, propagation, and service firewall/private endpoint access. The backend, not the browser or agent identity, needs Search permissions. |
| 404 from Foundry | Verify the project endpoint, deployed embedding name and saved prompt-agent name/version. Hub/classic agent SDK patterns are not interchangeable. |
| Embedding endpoint error | Use the resource's Azure OpenAI endpoint, not the project endpoint; verify that the embedding deployment exists in that resource and supports API version `2024-10-21`. |
| Agent rejects schema or tool choice | Choose an agent/model that supports Responses structured outputs. Do not replace this with a chat-completions-only endpoint. |
| Search schema/dimension error | Create the dedicated index exactly from `search-index.json`; use `text-embedding-3-small` with 1536 dimensions. Do not alter the demo index. |
| 503 / replacement failed | Read server logs; fix RBAC, quota, service availability, disk permission, or per-record indexing failure. Retry upload; Q&A stays disabled until it succeeds. Do not discard the ledger. |
| No extractable text / OCR message | Use a text-based PDF; OCR and encrypted PDFs are unsupported. |
| Unexpected unsupported answer | Wait briefly after indexing, rephrase a specific question, and check whether the PDF contains text evidence. Retrieval is limited to five chunks. |
| 409 from Q&A | Another tab replaced the PDF; refresh to get the active document UUID. |
| 403 from this app | Use the same loopback origin, no reverse proxy. The sample intentionally rejects remote clients, foreign origins and foreign Host headers. |

Do not upload confidential material into an unapproved Azure environment. Server
logs can contain provider error details; keep them private. There is no production
hardening, OCR, multi-user isolation, telemetry service, document download viewer,
or automated provisioning in this starter kit.
