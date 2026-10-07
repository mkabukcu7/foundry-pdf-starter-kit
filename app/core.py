import json
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Callable, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_BYTES = 5 * 1024 * 1024
MAX_PAGES = 50
MAX_CHUNKS = 500
MAX_DOCUMENTS = 2
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
    def read_pages(self, data: bytes, pages: list[int]) -> dict[int, str]: ...
    def index(self, chunks: list[Chunk]) -> None: ...
    def delete(self, keys: list[str]) -> None: ...
    def retrieve(self, question: str, document_id: str) -> list[Chunk]: ...
    def select(self, question: str, chunks: list[Chunk]) -> Selection: ...


def extract_chunks(
    data: bytes, filename: str, content_type: str,
    ocr: Callable[[bytes, list[int]], dict[int, str]] | None = None,
) -> list[Chunk]:
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
        texts = []
        ocr_pages = []
        image_pages = set()
        for page_number, page in enumerate(reader.pages, start=1):
            text = " ".join((page.extract_text() or "").split())
            texts.append(text)
            # Include image-bearing pages even when they also have a text layer.
            if len(page.images):
                image_pages.add(page_number)
            if not text or page_number in image_pages:
                ocr_pages.append(page_number)
    except (PdfReadError, ValueError, KeyError, TypeError, IndexError, RecursionError) as error:
        if isinstance(error, UploadError):
            raise
        raise UploadError("Unable to read this PDF; upload a valid PDF.") from error
    if ocr_pages:
        if ocr is None:
            raise UploadError("This PDF requires OCR; configure Document Intelligence before uploading it.")
        recognized = ocr(data, ocr_pages)
        if set(recognized) != set(ocr_pages):
            raise ServiceError("OCR did not return every requested page; document was not indexed.")
        for page_number in ocr_pages:
            text = " ".join(recognized[page_number].split())
            if not text and (texts[page_number - 1] or page_number in image_pages):
                raise UploadError(
                    f"OCR found no readable text on image-bearing page {page_number}; "
                    "upload a clearer scan or remove the blank image page."
                )
            texts[page_number - 1] = text
    document_id = uuid4().hex
    chunks = []
    for page_number, text in enumerate(texts, start=1):
        for start in range(0, len(text), 1050):
            chunks.append(Chunk(
                f"{document_id}-{len(chunks)}", document_id,
                filename, page_number, text[start:start + 1200],
            ))
            if len(chunks) > MAX_CHUNKS:
                raise UploadError("PDF exceeds the 500-chunk text limit.")
            if start + 1200 >= len(text):
                break
    if not chunks:
        raise UploadError("No readable text found. OCR cannot read a blank or illegible PDF.")
    return chunks


class Library:
    """One local user, one document set, one process. Persist keys before Azure writes."""

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
                        or set(self.state["active"]) not in (
                            {"document_id", "document_name", "chunks"},
                            {"document_id", "document_name", "chunks", "documents"},
                        )
                    )
                )
            ):
                raise ServiceError("Invalid runtime state; do not discard the indexed-key ledger.")
            active = self.state["active"]
            if active is not None and "documents" in active:
                documents = active["documents"]
                if (
                    not isinstance(documents, list) or not 1 <= len(documents) <= MAX_DOCUMENTS
                    or any(
                        not isinstance(doc, dict)
                        or set(doc) != {"document_id", "document_name", "chunks"}
                        or not isinstance(doc["document_id"], str)
                        or not isinstance(doc["document_name"], str)
                        or not isinstance(doc["chunks"], int) or doc["chunks"] <= 0
                        for doc in documents
                    )
                    or len({doc["document_id"] for doc in documents}) != len(documents)
                    or len({doc["document_name"].casefold() for doc in documents}) != len(documents)
                    or sum(doc["chunks"] for doc in documents) != active["chunks"]
                ):
                    raise ServiceError("Invalid document set state; preserve the indexed-key ledger.")

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
        return self.upload_many([(data, filename, content_type)])

    def upload_many(self, files: list[tuple[bytes, str, str]]) -> dict:
        if not 1 <= len(files) <= MAX_DOCUMENTS:
            raise UploadError("Select one or two PDFs to upload together.")
        if len({filename.casefold() for _, filename, _ in files}) != len(files):
            raise UploadError("Use distinct filenames so citations identify each PDF.")
        chunks = []
        documents = []
        for data, filename, content_type in files:
            extracted = extract_chunks(data, filename, content_type, self.gateway.read_pages)
            documents.append({
                "document_id": extracted[0].document_id,
                "document_name": filename,
                "chunks": len(extracted),
            })
            chunks.extend(extracted)
            if len(chunks) > MAX_CHUNKS:
                raise UploadError("The document set exceeds the 500-chunk text limit.")
        with self.lock:
            # Invalidate first. A failed replacement must never expose old content.
            self.state["active"] = None
            self.save()
            self.gateway.delete(self.state["keys"])
            self.state["keys"] = [chunk.id for chunk in chunks]
            self.save()
            self.gateway.index(chunks)
            self.state["active"] = documents[0] if len(documents) == 1 else {
                "document_id": uuid4().hex,
                "document_name": " + ".join(doc["document_name"] for doc in documents),
                "chunks": len(chunks), "documents": documents,
            }
            self.save()
            return self.state["active"]

    def answer(self, question: str, document_id: str) -> dict:
        with self.lock:
            active = self.state["active"]
            if active is None or active["document_id"] != document_id:
                raise StateConflict("Upload a PDF or refresh: this document is no longer current.")
            chunks = []
            for document in active.get("documents", [active]):
                retrieved = self.gateway.retrieve(question, document["document_id"])
                # Validate each retrieval independently, not just membership in the set.
                if any(
                    c.document_id != document["document_id"]
                    or c.document_name != document["document_name"] for c in retrieved
                ):
                    raise ServiceError("Retrieval returned content outside the current document.")
                chunks.extend(retrieved)
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
