"""Web uploads put text into the prompt only up to MAX_INLINE_TEXT_SIZE per
message. A file that doesn't fit reaches the agent as its saved path."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from nerve.gateway.routes.files import MAX_INLINE_TEXT_SIZE
from nerve.gateway.server import _load_uploaded_files


class _FakeDB:
    def __init__(self, records: list[dict]):
        self.records = records

    async def get_uploaded_files_by_ids(self, file_ids: list[str]) -> list[dict]:
        return [r for r in self.records if r["id"] in file_ids]


def _record(
    tmp_path: Path, file_id: str, filename: str, data: bytes,
    file_type: str = "text", media_type: str = "text/plain",
) -> dict:
    path = tmp_path / f"{file_id}_{filename}"
    path.write_bytes(data)
    return {
        "id": file_id, "filename": filename, "disk_path": str(path),
        "file_type": file_type, "media_type": media_type,
    }


async def _load(records: list[dict]) -> tuple[list[dict], list[dict]]:
    return await _load_uploaded_files(_FakeDB(records), [r["id"] for r in records])


@pytest.mark.asyncio
async def test_small_text_file_is_inlined(tmp_path):
    rec = _record(tmp_path, "f1", "notes.txt", b"hello\nworld\n")
    images, refs = await _load([rec])
    assert images == [{"type": "text_file", "filename": "notes.txt", "content": "hello\nworld\n"}]
    assert refs == [{
        "type": "file", "url": "/api/files/uploads/f1",
        "filename": "notes.txt", "media_type": "text/plain",
    }]


@pytest.mark.asyncio
async def test_file_at_the_limit_is_inlined(tmp_path):
    body = b"y" * MAX_INLINE_TEXT_SIZE
    images, _ = await _load([_record(tmp_path, "f1", "edge.txt", body)])
    assert images[0]["content"] == body.decode()


@pytest.mark.asyncio
async def test_file_over_the_limit_goes_in_as_its_path(tmp_path):
    rec = _record(
        tmp_path, "f1", "big.json", b"x" * (MAX_INLINE_TEXT_SIZE + 1),
        media_type="application/json",
    )
    images, refs = await _load([rec])
    note = images[0]["content"]
    assert images[0]["type"] == "text_file"
    assert images[0]["filename"] == "big.json"
    assert note.startswith("[Not included: big.json is 128 KB, over the 128 KB inline limit.")
    assert f"Saved at {rec['disk_path']}." in note
    assert "xxx" not in note
    # The reference shown in the chat doesn't change.
    assert refs[0]["url"] == "/api/files/uploads/f1"


@pytest.mark.asyncio
async def test_file_over_the_limit_is_not_read(tmp_path, monkeypatch):
    rec = _record(tmp_path, "f1", "big.log", b"z" * (MAX_INLINE_TEXT_SIZE + 1))
    read: list[str] = []
    real = Path.read_bytes

    def spy(self):
        read.append(str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    await _load([rec])
    assert rec["disk_path"] not in read


@pytest.mark.asyncio
async def test_limit_is_per_message(tmp_path):
    half = MAX_INLINE_TEXT_SIZE // 2 + 1  # each fits alone, not both together
    first = _record(tmp_path, "f1", "a.txt", b"a" * half)
    second = _record(tmp_path, "f2", "b.txt", b"b" * half)
    third = _record(tmp_path, "f3", "c.txt", b"small")
    images, refs = await _load([first, second, third])
    assert images[0]["content"] == "a" * half
    note = images[1]["content"]
    assert note.startswith(
        "[Not included: b.txt is 64 KB, and with the files before it "
        "the message would pass its 128 KB inline limit."
    )
    assert f"Saved at {second['disk_path']}." in note
    assert images[2]["content"] == "small"
    assert [r["filename"] for r in refs] == ["a.txt", "b.txt", "c.txt"]


@pytest.mark.asyncio
async def test_binary_file_note_has_its_path(tmp_path):
    rec = _record(
        tmp_path, "f1", "dump.tar", b"\xff\xfe\x00\x01" * 10,
        media_type="application/x-tar",
    )
    images, _ = await _load([rec])
    assert images[0]["content"] == f"[Binary file: dump.tar, 40 bytes. Saved at {rec['disk_path']}]"


@pytest.mark.asyncio
async def test_binary_file_leaves_the_budget_to_text(tmp_path):
    binary = _record(tmp_path, "f1", "blob.bin", b"\xff" * (MAX_INLINE_TEXT_SIZE - 10))
    text = _record(tmp_path, "f2", "t.txt", b"t" * 100)
    images, _ = await _load([binary, text])
    assert images[0]["content"].startswith("[Binary file: blob.bin,")
    assert images[1]["content"] == "t" * 100


@pytest.mark.asyncio
async def test_images_and_pdfs_are_unchanged(tmp_path):
    png = _record(
        tmp_path, "f1", "a.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_INLINE_TEXT_SIZE + 10),
        file_type="image", media_type="image/png",
    )
    pdf = _record(
        tmp_path, "f2", "a.pdf", b"%PDF-1.4\n%%EOF\n",
        file_type="pdf", media_type="application/pdf",
    )
    text = _record(tmp_path, "f3", "t.txt", b"after the images")
    images, refs = await _load([png, pdf, text])
    assert [i["type"] for i in images] == ["base64", "base64", "text_file"]
    assert base64.b64decode(images[0]["data"]) == Path(png["disk_path"]).read_bytes()
    assert images[0]["media_type"] == "image/png"
    assert base64.b64decode(images[1]["data"]) == b"%PDF-1.4\n%%EOF\n"
    assert images[1]["media_type"] == "application/pdf"
    # Images and PDFs don't count against the text limit.
    assert images[2]["content"] == "after the images"
    assert [r["type"] for r in refs] == ["image", "file", "file"]
