import logging
from contextlib import asynccontextmanager
from pathlib import Path

from azure.core.exceptions import AzureError
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from openai import OpenAIError
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from .azure import AzureGateway
from .core import MAX_BYTES, MAX_DOCUMENTS, Library, ServiceError, StateConflict, UploadError

ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)


class Question(BaseModel):
    document_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Question cannot be blank.")
        return value.strip()


def create_app(library: Library | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if library is not None:
            app.state.library = library
            yield
            return
        load_dotenv(ROOT / ".env", override=False)
        gateway = AzureGateway()
        try:
            app.state.library = Library(gateway, ROOT / ".runtime" / "state.json")
            yield
        finally:
            gateway.close()

    app = FastAPI(title="Propel PDF Knowledge Librarian", lifespan=lifespan)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if (
            not request.client
            or request.client.host not in {"127.0.0.1", "::1"}
            or request.url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or (request.headers.get("origin") and request.headers["origin"] != str(request.base_url).rstrip("/"))
        ):
            return JSONResponse(status_code=403, content={"detail": "Local, same-origin use only; no browser sign-in is provided."})
        if request.url.path == "/api/upload" and request.method == "POST":
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                length = 0
            if length <= 0:
                return JSONResponse(status_code=411, content={"detail": "Uploads require Content-Length (browser FormData supplies it)."})
            if length > MAX_BYTES * MAX_DOCUMENTS + 64 * 1024:
                return JSONResponse(status_code=413, content={"detail": "Upload exceeds the two-PDF, 10 MiB total limit."})
        return await call_next(request)

    @app.exception_handler(ServiceError)
    @app.exception_handler(AzureError)
    @app.exception_handler(OpenAIError)
    @app.exception_handler(OSError)
    async def service_error(request: Request, error: Exception):
        logger.error("Starter kit operation failed", exc_info=error)
        return JSONResponse(
            status_code=503,
            content={"detail": "Operation failed; check server logs and Azure configuration. A failed replacement disables Q&A until upload succeeds."},
        )

    @app.get("/")
    def home():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/api/document")
    def document(request: Request):
        return {"document": request.app.state.library.current()}

    @app.post("/api/upload")
    async def upload(request: Request, file: list[UploadFile] = File(...)):
        try:
            if not 1 <= len(file) <= MAX_DOCUMENTS:
                raise UploadError("Select one or two PDFs to upload together.")
            files = []
            for item in file:
                data = await item.read(MAX_BYTES + 1)
                if not data or len(data) > MAX_BYTES:
                    raise UploadError("Each PDF must be non-empty and no larger than 5 MiB.")
                files.append((data, item.filename or "", item.content_type or ""))
            return await run_in_threadpool(
                request.app.state.library.upload_many, files,
            )
        except UploadError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        finally:
            for item in file:
                await item.close()

    @app.post("/api/ask")
    def ask(body: Question, request: Request):
        try:
            return request.app.state.library.answer(body.question, body.document_id)
        except StateConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return app


app = create_app()
