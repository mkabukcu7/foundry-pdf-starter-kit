import pytest
from fastapi.testclient import TestClient

from app.core import Chunk, Evidence, Library, Selection, ServiceError, StateConflict, UploadError
from app.main import create_app
from samples.make_sample import make_pdf
from test_starter import FakeGateway


@pytest.fixture
def library(tmp_path):
    return Library(FakeGateway(), tmp_path / "state.json")


def pair():
    return [
        (make_pdf([["The first workshop lasts 45 minutes."]]), "first.pdf", "application/pdf"),
        (make_pdf([["Title."], ["The second workshop lasts 75 minutes."]]), "second.pdf", "application/pdf"),
    ]


def test_pair_answers_with_distinct_sources_and_pages(library):
    current = library.upload_many(pair())
    rows = list(library.gateway.rows.values())
    assert current["chunks"] == 3
    assert len(current["documents"]) == 2
    first, _, second = rows
    library.gateway.selection = Selection(supported=True, evidence=[
        Evidence(chunk_id=first.id, quote=first.content),
        Evidence(chunk_id=second.id, quote=second.content),
    ])
    answer = library.answer("How long is each workshop?", current["document_id"])
    assert [(c["document_name"], c["page"]) for c in answer["citations"]] == [
        ("first.pdf", 1), ("second.pdf", 2),
    ]
    assert len(library.gateway.selected_chunks) == 3
    restarted = Library(library.gateway, library.state_path)
    assert restarted.current() == current
    assert restarted.answer("How long is each?", current["document_id"]) == answer


def test_invalid_second_file_preserves_previous_set(library):
    current = library.upload_many(pair())
    rows = dict(library.gateway.rows)
    with pytest.raises(UploadError):
        library.upload_many([pair()[0], (b"bad", "bad.pdf", "application/pdf")])
    assert library.current() == current
    assert library.gateway.rows == rows


@pytest.mark.parametrize("files", [[], pair() + [pair()[0]], [pair()[0], pair()[0]]])
def test_invalid_count_or_duplicate_names_are_rejected(library, files):
    with pytest.raises(UploadError):
        library.upload_many(files)
    assert not library.gateway.rows


def test_set_replacement_cleans_both_sources_and_rejects_stale_id(library):
    old = library.upload_many(pair())
    keys = set(library.gateway.rows)
    new = library.upload(*pair()[0])
    assert "documents" not in new
    assert keys.isdisjoint(library.gateway.rows)
    with pytest.raises(StateConflict):
        library.answer("How long?", old["document_id"])


@pytest.mark.parametrize("failure", ["fail_delete", "fail_index"])
def test_set_write_failure_preserves_cleanup_ledger(library, failure):
    library.upload_many(pair())
    setattr(library.gateway, failure, True)
    with pytest.raises(ServiceError):
        library.upload_many(pair())
    restarted = Library(library.gateway, library.state_path)
    assert restarted.current() is None
    setattr(library.gateway, failure, False)
    newest = restarted.upload_many(pair())
    assert set(library.gateway.rows) == set(restarted.state["keys"])
    assert {c.document_id for c in library.gateway.rows.values()} == {
        doc["document_id"] for doc in newest["documents"]
    }


def test_set_retrieval_checks_each_source(library):
    current = library.upload_many(pair())
    library.gateway.retrieve = lambda question, document_id: [
        Chunk("bad", current["documents"][1]["document_id"], "second.pdf", 1, "Unscoped.")
    ]
    with pytest.raises(ServiceError, match="outside"):
        library.answer("Question?", current["document_id"])


def test_total_chunk_limit_preserves_previous_set(library, monkeypatch):
    current = library.upload(*pair()[0])
    monkeypatch.setattr("app.core.MAX_CHUNKS", 2)
    with pytest.raises(UploadError, match="document set"):
        library.upload_many(pair())
    assert library.current() == current


def test_http_pair_count_limits_and_status(library):
    with TestClient(create_app(library), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        files = [("file", (name, data, mime)) for data, name, mime in pair()]
        response = client.post("/api/upload", files=files)
        assert response.status_code == 200
        assert client.get("/api/document").json()["document"] == response.json()
        assert client.post("/api/upload", files=files + [files[0]]).status_code == 400
        assert library.current() == response.json()
