import json
import os
from dataclasses import asdict
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.models import VectorizedQuery
from pydantic import ValidationError
from openai import AzureOpenAI

from .core import Chunk, Selection, ServiceError


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or "<" in value:
        raise ServiceError(f"Set {name} in .env before starting the app.")
    return value


class AzureGateway:
    def __init__(self):
        self.credential = DefaultAzureCredential()
        self.project = AIProjectClient(
            endpoint=required("FOUNDRY_PROJECT_ENDPOINT"), credential=self.credential,
        )
        self.openai = self.project.get_openai_client()
        self.agent = {
            "type": "agent_reference",
            "name": required("FOUNDRY_AGENT_NAME"),
            "version": required("FOUNDRY_AGENT_VERSION"),
        }
        self.embedding_model = required("FOUNDRY_EMBEDDING_MODEL")
        self.embedding_client = AzureOpenAI(
            azure_endpoint=required("FOUNDRY_EMBEDDING_ENDPOINT"),
            azure_ad_token_provider=get_bearer_token_provider(
                self.credential, "https://cognitiveservices.azure.com/.default",
            ),
            api_version="2024-10-21",
        )
        endpoint, index = required("SEARCH_ENDPOINT"), required("SEARCH_INDEX_NAME")
        self.search = SearchClient(endpoint, index, self.credential)
        self.indexes = SearchIndexClient(endpoint, self.credential)
        schema = self.indexes.get_index(index)
        fields = {field.name: field for field in schema.fields}
        expected = {"id", "document_id", "document_name", "page", "content", "vector"}
        if not expected.issubset(fields) or fields["vector"].vector_search_dimensions != 1536:
            raise ServiceError("Search index does not match search-index.json (1536 dimensions).")
        if not fields["document_id"].filterable:
            raise ServiceError("Search document_id must be filterable.")

    def close(self) -> None:
        self.search.close()
        self.indexes.close()
        self.openai.close()
        self.embedding_client.close()
        self.project.close()
        self.credential.close()

    def embeddings(self, texts: list[str]) -> list[list[float]]:
        response = self.embedding_client.embeddings.create(
            model=self.embedding_model, input=texts, dimensions=1536,
        )
        vectors = [item.embedding for item in sorted(response.data, key=lambda item: item.index)]
        if len(vectors) != len(texts) or any(len(vector) != 1536 for vector in vectors):
            raise ServiceError("Embedding count or dimensions do not match the Search schema.")
        return vectors

    @staticmethod
    def check_results(results, expected: int) -> None:
        if len(results) != expected or any(not result.succeeded for result in results):
            raise ServiceError("Search reported a partial indexing/deletion failure; retry upload.")

    def delete(self, keys: list[str]) -> None:
        if keys:
            self.check_results(
                self.search.delete_documents([{"id": key} for key in keys]), len(keys),
            )

    def index(self, chunks: list[Chunk]) -> None:
        for start in range(0, len(chunks), 16):
            batch = chunks[start:start + 16]
            vectors = self.embeddings([chunk.content for chunk in batch])
            documents = [{**asdict(chunk), "vector": vector} for chunk, vector in zip(batch, vectors, strict=True)]
            self.check_results(self.search.upload_documents(documents), len(documents))

    def retrieve(self, question: str, document_id: str) -> list[Chunk]:
        vector = self.embeddings([question])[0]
        # UUIDs are generated locally; escaping also keeps this method safe in isolation.
        document_id = document_id.replace("'", "''")
        results = self.search.search(
            search_text=question,
            filter=f"document_id eq '{document_id}'",
            vector_filter_mode="preFilter",
            vector_queries=[VectorizedQuery(vector=vector, fields="vector", k_nearest_neighbors=5)],
            select=["id", "document_id", "document_name", "page", "content"],
            top=5,
        )
        return [Chunk(**{key: row[key] for key in Chunk.__dataclass_fields__}) for row in results]

    def select(self, question: str, chunks: list[Chunk]) -> Selection:
        response = self.openai.responses.create(
            instructions=(Path(__file__).resolve().parents[1] / "agent-instructions.txt").read_text(encoding="utf-8"),
            input=[{
                "role": "user",
                "content": json.dumps({
                    "question": question,
                    "untrusted_retrieved_passages": [asdict(chunk) for chunk in chunks],
                }),
            }],
            extra_body={"agent_reference": self.agent},
            tool_choice="none",
            text={"format": {
                "type": "json_schema", "name": "grounded_selection", "strict": True,
                "schema": Selection.model_json_schema(),
            }},
            store=False,
        )
        if response.status != "completed":
            raise ServiceError("Foundry did not complete the answer.")
        try:
            return Selection.model_validate_json(response.output_text)
        except ValidationError as error:
            raise ServiceError("Foundry returned invalid evidence JSON.") from error
