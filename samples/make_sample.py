"""Regenerate the synthetic PDF using only the pinned runtime dependency."""
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject, DictionaryObject, NameObject,
)

PAGES = [
    [
        "Propel Moonflower Workshop - Synthetic Guide",
        "This fictional guide contains no customer information.",
        "The Moonflower workshop lasts 45 minutes.",
        "Participants need a laptop and a text-based PDF.",
    ],
    [
        "Workshop agenda",
        "Spend 10 minutes introducing grounding and citations.",
        "Spend 20 minutes uploading a PDF and asking questions.",
        "Spend 15 minutes reviewing answers and unsupported questions.",
        "The workshop owner is the Propel Enablement team.",
    ],
]


def make_pdf(pages: list[list[str]]) -> bytes:
    from io import BytesIO

    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font)
    for lines in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
        })
        commands = ["BT /F1 12 Tf 50 740 Td 20 TL"]
        for line in lines:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"({escaped}) Tj T*")
        commands.append("ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


if __name__ == "__main__":
    Path(__file__).with_name("moonflower.pdf").write_bytes(make_pdf(PAGES))
