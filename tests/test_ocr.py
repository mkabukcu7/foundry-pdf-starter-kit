from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from app.azure import AzureGateway
from app.core import Library, ServiceError, UploadError, extract_chunks
from samples.make_sample import make_pdf
from test_starter import FakeGateway


def image_pdf(with_text=False, mixed=False):
    writer = PdfWriter()
    reader = PdfReader(BytesIO(make_pdf([["Native page one."], ["Native page two." if with_text else ""]])))
    if mixed:
        writer.add_page(reader.pages[0])
    page = writer.add_page(reader.pages[1])
    image = DecodedStreamObject()
    image.set_data(b"\x00\x00\x00")
    image.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(1),
        NameObject("/Height"): NumberObject(1),
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
        NameObject("/BitsPerComponent"): NumberObject(8),
    })
    page["/Resources"][NameObject("/XObject")] = DictionaryObject({
        NameObject("/Scan"): writer._add_object(image),
    })
    stream = DecodedStreamObject()
    stream.set_data(page.get_contents().get_data() + b"\nq 100 0 0 100 50 500 cm /Scan Do Q")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_text_pdf_does_not_call_ocr():
    ocr = Mock()
    chunks = extract_chunks(make_pdf([["Text only."]]), "text.pdf", "application/pdf", ocr)
    assert chunks[0].content == "Text only."
    ocr.assert_not_called()


@pytest.mark.parametrize("with_text,mixed", [(False, False), (False, True), (True, True)])
def test_scanned_and_mixed_pages_keep_original_page_numbers(with_text, mixed):
    data = image_pdf(with_text, mixed)
    number = 2 if mixed else 1
    ocr = Mock(return_value={number: "Recognized scan text."})
    chunks = extract_chunks(data, "scan.pdf", "application/pdf", ocr)
    ocr.assert_called_once_with(data, [number])
    assert chunks[-1].page == number
    assert chunks[-1].content == "Recognized scan text."
    if mixed:
        assert chunks[0].content == "Native page one."
        assert chunks[0].page == 1


@pytest.mark.parametrize("recognized", [{}, {1: "Wrong page."}, {2: "text", 3: "Extra page."}])
def test_ocr_cannot_silently_omit_or_add_pages(recognized):
    with pytest.raises(ServiceError, match="every requested page"):
        extract_chunks(image_pdf(mixed=True), "mixed.pdf", "application/pdf", Mock(return_value=recognized))


def test_unreadable_image_page_does_not_get_silently_skipped():
    with pytest.raises(UploadError, match="page 2"):
        extract_chunks(image_pdf(mixed=True), "mixed.pdf", "application/pdf", Mock(return_value={2: ""}))


def test_blank_pages_can_remain_blank():
    ocr = Mock(return_value={1: ""})
    chunks = extract_chunks(make_pdf([[], ["Page two."]]), "blank.pdf", "application/pdf", ocr)
    assert len(chunks) == 1
    assert chunks[0].page == 2


def test_ocr_error_preserves_current_document(tmp_path):
    gateway = FakeGateway()
    library = Library(gateway, tmp_path / "state.json")
    original = library.upload(make_pdf([["Original."]]), "original.pdf", "application/pdf")
    gateway.read_pages = Mock(side_effect=ServiceError("OCR unavailable"))
    with pytest.raises(ServiceError, match="OCR unavailable"):
        library.upload(image_pdf(), "scan.pdf", "application/pdf")
    assert library.current() == original
    assert len(gateway.rows) == 1


def test_gateway_requests_selected_pages_and_uses_unicode_spans():
    gateway = AzureGateway.__new__(AzureGateway)
    gateway.ocr_client = Mock()
    poller = gateway.ocr_client.begin_analyze_document.return_value
    poller.done.return_value = True
    poller.result.return_value = SimpleNamespace(
        content="First. Scanned text.",
        pages=[SimpleNamespace(page_number=2, spans=[SimpleNamespace(offset=7, length=13)])],
    )
    assert gateway.read_pages(b"pdf", [2]) == {2: "Scanned text."}
    args, kwargs = gateway.ocr_client.begin_analyze_document.call_args
    assert args == ("prebuilt-read",)
    assert kwargs["pages"] == "2"
    assert kwargs["string_index_type"] == "unicodeCodePoint"
    assert kwargs["content_type"] == "application/pdf"
    assert kwargs["body"].getvalue() == b"pdf"
    poller.result.assert_called_once_with(timeout=180)


def test_ocr_timeout_is_explicit():
    gateway = AzureGateway.__new__(AzureGateway)
    gateway.ocr_client = Mock()
    gateway.ocr_client.begin_analyze_document.return_value.done.return_value = False
    with pytest.raises(ServiceError, match="timed out"):
        gateway.read_pages(b"pdf", [1])


@pytest.mark.parametrize("offset,length", [(-1, 1), (0, -1), (0, 100), (None, 1)])
def test_ocr_invalid_spans_are_rejected(offset, length):
    gateway = AzureGateway.__new__(AzureGateway)
    gateway.ocr_client = Mock()
    poller = gateway.ocr_client.begin_analyze_document.return_value
    poller.done.return_value = True
    poller.result.return_value = SimpleNamespace(
        content="Text.",
        pages=[SimpleNamespace(page_number=1, spans=[SimpleNamespace(offset=offset, length=length)])],
    )
    with pytest.raises(ServiceError, match="invalid text spans"):
        gateway.read_pages(b"pdf", [1])
