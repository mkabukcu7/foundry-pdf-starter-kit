# Technical guide

Start with the [README](../README.md) for the customer setup and demo sequence.
This reference covers implementation details and deeper troubleshooting.
For a new-resource setup, the opt-in
[provisioning script and guide](provisioning.md) create resources and populate
`.env`; manual setup below remains supported.

## Azure setup details

Create a dedicated Search index through the portal's index JSON editor, or use
the Search data-plane REST API:
`PUT /indexes/<index>?api-version=2024-07-01`, with
[`search-index.json`](../search-index.json) and an Entra bearer token for
`https://search.azure.com`. Replace the index-name placeholder first.
The app reads the schema during startup; it does not create or update it.

Use a current Foundry project supporting versioned prompt agents and the Responses
API, not a classic hub-only endpoint. The backend retrieves Search evidence itself,
so no Search-to-Foundry connection or Search role assignment to the agent identity
is needed.

Document Intelligence must support `prebuilt-read` and Entra authentication
through a custom-subdomain endpoint. S0 avoids the free tier's two-page analysis
limit. No OCR model training, deployment, or blob storage is needed.

## Agent configuration and SDK compatibility

The explicit `python -m app.setup_agent` command creates an agent version using
the Azure CLI identity and verifies the saved definition. It refuses to version
an existing agent unless `--new-version` is supplied. This setup action is separate
from the running application, which never provisions or deletes Azure resources.

Instructions, disabled tool use, `temperature=0`, and the strict `Selection`
schema are saved on the agent definition. Choose an answer model supporting
these settings. When an agent is specified, Foundry does not allow request-level
`instructions` or `text` overrides; runtime requests contain the agent reference
and untrusted question/passages, with response storage disabled. If the prompt
or `Selection` schema changes, create a new agent version and update `.env`.
Restart the server after code or configuration changes.

Agent invocation uses `AIProjectClient.get_openai_client()` and the Responses
API `agent_reference` pattern. Embeddings use a separate `AzureOpenAI` client,
the resource's deployment endpoint, Entra tokens, and API version `2024-10-21`.
The deployment is `text-embedding-3-small` with 1536 dimensions. Do not interchange
the Foundry project and Azure OpenAI resource URLs.

Dependencies are pinned, including the OpenAI client. Offline SDK compatibility
tests cover client construction, request serialization, Search schema
deserialization, and vector-query parameters. These cannot establish live
model availability, RBAC, service behavior, or agent configuration.

## Ingestion and retrieval

Upload validation checks extension, MIME type, PDF signature, encryption, size,
page count, and chunk count. One or two distinct filenames can be uploaded
together; duplicate filenames are rejected case-insensitively. HTTP uploads use
repeated `file` multipart fields. The request limit is 10 MiB plus 64 KiB overhead,
and each PDF is limited to 5 MiB and 50 pages, with 500 chunks across the set.

Text-only pages use local `pypdf` extraction. Image-bearing and textless pages
use Document Intelligence `prebuilt-read`, including pages with both images
and a text layer. OCR replaces local text on those pages to avoid duplication.
All requested pages must be returned, with valid content spans and page metadata.
Missing results or service failures reject the upload. Blank pages without images
may remain empty; unreadable image pages are rejected. Polling waits up to 180
seconds; a timed-out cloud analysis is not cancelled and may still incur charges.

Normalized text is chunked within each page (1200 characters, 150-character
overlap). Embeddings are generated in batches of 16. Search records contain
content, vectors, document UUID/name, physical page number, and chunk ID.
Raw PDFs and extracted text are not stored locally.

Each question uses hybrid keyword/vector retrieval with a `document_id`
pre-filter and up to five results per PDF (ten for a pair). Each PDF has its own
UUID; pairs also have a set UUID for stale-tab checks. Each retrieval is checked
against the requested PDF's UUID and filename before evidence is combined.

The agent selects up to three verbatim quotes or abstains. The backend validates
chunk IDs and exact quote membership, then constructs filenames/pages from indexed
metadata. Invalid model output is an explicit service error, not an unsupported
answer. Exact quote validation does not independently prove semantic support;
the model still judges whether the quote answers the question.

Questions, filenames, and passages are serialized as untrusted data. The agent
has no tools, no prior chat turns are sent, and the browser renders text rather
than HTML. These are defense-in-depth measures, not a guarantee of prompt-injection
resistance. Evaluate real and adversarial documents before extending the sample.

## Replacement and recovery

Both PDFs are extracted and validated before the current set is invalidated.
If either fails extraction or OCR, the previous set remains usable.
A process lock then serializes indexed replacement and Q&A:

1. Invalidate the active set and persist the state.
2. Delete all known old chunk keys, checking per-record results.
3. Persist new chunk keys before any indexing writes.
4. Upload and check all records; activate the new document set only on success.

The ignored `.runtime/state.json` file is an atomic local ledger of the active
set and known Search keys. A failed delete or partial indexing operation disables
Q&A and retains keys for cleanup on the next upload, including after a restart.
Search writes are eventually consistent; new UUIDs prevent stale records from
being retrieved for the replacement. Old tabs receive HTTP 409 after replacement.

Single-PDF state remains compatible with earlier runtime ledgers and API clients.
Use exactly one process/app copy per dedicated index. Preserve the ledger: if
lost, have an administrator reconcile orphaned Search records before reusing
the index. Shutdown does not clean the index or delete Azure resources.

## Development and debugging

VS Code **Terminal > Run Task > Run PDF demo** uses the included Windows-oriented
task to run the backend in an integrated terminal. **F5 > Debug PDF demo** uses
the Python Debugger extension and the local `.venv`. Adapt interpreter paths
for other operating systems. Stop an existing server with Ctrl+C before using
port 8000 again.

These configurations debug FastAPI, not a locally hosted agent or Agent Inspector.
The agent is a hosted prompt definition in Foundry.

Regenerate the synthetic sample with `python samples/make_sample.py`.
It uses the pinned `pypdf` dependency rather than adding a PDF-generation package.
[`samples/questions.txt`](../samples/questions.txt) records exact expected evidence.
For test commands and verification history, see [testing.md](testing.md).

## Troubleshooting

| Symptom | Check |
|---|---|
| A setting is missing | Replace `.env` placeholders; environment variables take precedence. |
| Credential failure / 401 | Run `az login` in the correct tenant. Stale environment/service-principal credentials can take precedence over CLI login in `DefaultAzureCredential`. |
| 403 from Azure | Check role scope, propagation, and firewall/private endpoint access. The backend identity needs Search and OCR permissions. |
| 404 from Foundry | Verify project URL, deployment names, and saved agent name/version. Classic/hub agent patterns are not interchangeable. |
| Embedding endpoint error | Use the resource's Azure OpenAI URL, not the project URL; confirm the embedding deployment supports API version `2024-10-21`. |
| Agent schema/tool-choice error | Configure instructions, strict schema, and disabled tools using `app.setup_agent`, not Responses overrides. Choose a compatible answer model. |
| Search schema/dimension error | Create the dedicated index from `search-index.json`; use 1536-dimensional `text-embedding-3-small`. |
| 503 / replacement failed | Read server logs; fix RBAC, quota, availability, disk permissions, or per-record indexing failures. Retry upload and preserve the ledger. |
| OCR endpoint / 401 / 403 error | Check the custom-subdomain endpoint, Entra analysis permissions, networking, and an S0 OCR-capable resource. Text-only PDFs do not invoke OCR. |
| No readable OCR text | Try a clearer scan; remove blank image pages. Fully blank, illegible, and encrypted PDFs are unsupported. |
| Unexpected unsupported answer | Wait briefly after indexing, then rephrase specifically. Retrieval is limited to five passages per PDF and may miss evidence. |
| Q&A returns 409 | Another upload replaced the set; refresh the tab. |
| Local app returns 403 | Use the same loopback origin without a reverse proxy. Remote clients, foreign origins, and foreign Host headers are rejected. |

Use only approved data and Azure environments. Keep server logs private because
provider error details may be sensitive. The app has no production hardening,
multi-user isolation, telemetry service, or document download viewer.

## Origin and reuse

The sample was extracted from
[mkabukcu7/sharepoint-foundry](https://github.com/mkabukcu7/sharepoint-foundry),
but imports nothing from it.

Its Search service informed chunking, hybrid `VectorizedQuery`, authentication,
and per-record result checks. Its Knowledge Librarian provider informed Foundry
agent invocation, and its librarian prompt/guardrails informed untrusted-data
and insufficient-evidence rules. Its local chat informed loopback restrictions.
The original regex PDF extraction was replaced with page-aware `pypdf`.
