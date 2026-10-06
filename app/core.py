import json
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_BYTES = 5 * 1024 * 1024
MAX_PAGES = 50
MAX_CHUNKS = 500
UNSUPPORTED = "The document does not support an answer to this question."


class UploadError(ValueError):
    pass


class StateConflict(ValueError):
    pass


class ServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Chunk:
    id: str
    document_id: str
    document_name: str
    page: int
    content: str


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk_id: str
    quote: str = Field(min_length=1, max_length=1200)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supported: bool
    evidence: list[Evidence] = Field(max_length=3)


class Gateway(Protocol):
    def index(self, chunks: list[Chunk]) -> None: ...
    def delete(self, keys: list[str]) -> None: ...
    def retrieve(self, question: str, document_id: str) -> list[Chunk]: ...
    def select(self, question: str, chunks: list[Chunk]) -> Selection: ...


def extract_chunks(data: bytes, filename: str, content_type: str) -> list[Chunk]:
    if not filename or "/" in filename or "\\" in filename or len(filename) > 180:
        raise UploadError("Use a plain PDF filename of at most 180 characters.")
    if not filename.lower().endswith(".pdf") or content_type not in {
        "application/pdf", "application/octet-stream",
    }:
        raise UploadError("Upload a PDF file.")
    if not data or len(data) > MAX_BYTES:
        raise UploadError("PDF must be non-empty and no larger than 5 MiB.")
    if not data.startswith(b"%PDF-"):
        raise UploadError("The file is not a PDF.")
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise UploadError("Encrypted PDFs are not supported.")
        if not 1 <= len(reader.pages) <= MAX_PAGES:
            raise UploadError("PDF must contain 1 to 50 pages.")
        document_id = uuid4().hex
        chunks = []
        for page_number, page in enumerate(reader.pages, start=1):
            text = " ".join((page.extract_text() or "").split())
            for start in range(0, len(text), 1050):
                content = text[start:start + 1200]
                chunks.append(Chunk(
                    f"{document_id}-{len(chunks)}", document_id,
                    filename, page_number, content,
                ))
                if len(chunks) > MAX_CHUNKS:
                    raise UploadError("PDF exceeds the 500-chunk text limit.")
                if start + 1200 >= len(text):
                    break
    except (PdfReadError, ValueError, KeyError, TypeError, IndexError, RecursionError) as error:
        if isinstance(error, UploadError):
            raise
        raise UploadError("Unable to read this PDF; upload a valid text-based PDF.") from error
    if not chunks:
        raise UploadError("No extractable text. Scanned PDFs requiring OCR are outside this starter kit's scope.")
    return chunks


class Library:
    """One local user, one document, one process. Persist keys before Azure writes."""

    def __init__(self, gateway: Gateway, state_path: Path):
        self.gateway = gateway
        self.state_path = state_path
        self.lock = Lock()
        self.state = {"active": None, "keys": []}
        if state_path.exists():
            try:
                self.state = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise ServiceError("Invalid runtime state; preserve the indexed-key ledger for reconciliation.") from error
            if (
                not isinstance(self.state, dict)
                or set(self.state) != {"active", "keys"}
                or not isinstance(self.state["keys"], list)
                or any(not isinstance(key, str) for key in self.state["keys"])
                or (
                    self.state["active"] is not None
                    and (
                        not isinstance(self.state["active"], dict)
                        or set(self.state["active"]) != {"document_id", "document_name", "chunks"}
                    )
                )
            ):
                raise ServiceError("Invalid runtime state; do not discard the indexed-key ledger.")

    def save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.state), encoding="utf-8")
            temporary.replace(self.state_path)
        except OSError:
            self.state["active"] = None
            raise

    def current(self) -> dict | None:
        with self.lock:
            return self.state["active"]

    def upload(self, data: bytes, filename: str, content_type: str) -> dict:
        chunks = extract_chunks(data, filename, content_type)
        with self.lock:
            # Invalidate first. A failed replacement must never expose old content.
            self.state["active"] = None
            self.save()
            self.gateway.delete(self.state["keys"])
            self.state["keys"] = [chunk.id for chunk in chunks]
            self.save()
            self.gateway.index(chunks)
            self.state["active"] = {
                "document_id": chunks[0].document_id,
                "document_name": filename,
                "chunks": len(chunks),
            }
            self.save()
            return self.state["active"]

    def answer(self, question: str, document_id: str) -> dict:
        with self.lock:
            active = self.state["active"]
            if active is None or active["document_id"] != document_id:
                raise StateConflict("Upload a PDF or refresh: this document is no longer current.")
            chunks = self.gateway.retrieve(question, document_id)
            # Defense in depth even if a retrieval implementation ignores its filter.
            if any(c.document_id != document_id or c.document_name != active["document_name"] for c in chunks):
                raise ServiceError("Retrieval returned content outside the current document.")
            if not chunks:
                return {"supported": False, "answer": UNSUPPORTED, "citations": []}
            selection = self.gateway.select(question, chunks)
            if not selection.supported:
                if selection.evidence:
                    raise ServiceError("Agent returned evidence for an unsupported answer.")
                return {"supported": False, "answer": UNSUPPORTED, "citations": []}
            if not selection.evidence:
                raise ServiceError("Agent marked an answer supported without evidence.")
            sources = {chunk.id: chunk for chunk in chunks}
            citations = []
            for evidence in selection.evidence:
                chunk = sources.get(evidence.chunk_id)
                if chunk is None or not evidence.quote.strip() or evidence.quote not in chunk.content:
                    raise ServiceError("Agent returned an invalid citation or non-verbatim evidence.")
                citations.append({
                    "document_name": chunk.document_name,
                    "page": chunk.page,
                    "quote": evidence.quote,
                    "chunk_id": chunk.id,
                })
            return {
                "supported": True,
                "answer": "\n\n".join(
                    f'{c["quote"]} [{c["document_name"]}, page {c["page"]}]' for c in citations
                ),
                "citations": citations,
            }
