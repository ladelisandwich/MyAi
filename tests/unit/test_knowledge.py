import json
import os
from pathlib import Path

from fsutil import symlink_or_skip
from homemesh.knowledge.chunking import chunk_text
from homemesh.knowledge.extract import extract
from homemesh.knowledge.pathsafety import PathPolicy
from homemesh.knowledge.scanner import iter_files, scan
from homemesh.knowledge.vectors import VectorIndex, rrf
from homemesh.protocol.jobs import KnowledgeScanPayload, KnownDocument
from pdfutil import make_pdf


def test_text_markdown_code_json_csv(tmp_path):
    assert extract(Path("a.md"), b"# Title\n\nhello").status == "indexed"
    assert extract(Path("a.py"), b"print('x')\n").mime == "text/x-code"
    assert extract(Path("Dockerfile"), b"FROM python\n").status == "indexed"
    j = extract(Path("a.json"), b'{"k": 1}')
    assert j.status == "indexed" and j.error is None
    bad = extract(Path("b.json"), b'{"k": ')
    assert bad.status == "indexed" and "invalid JSON" in bad.error
    c = extract(Path("a.csv"), b"name,age\nAda,36\nAlan,41\n")
    assert c.csv_header == "name | age" and "name: Ada | age: 36" in c.text
    assert extract(Path("x.bin"), b"\x00\x01").status == "unsupported"
    assert extract(Path("x.txt"), b"abc\x00def").status == "unsupported"
    assert extract(Path("e.txt"), b"   \n").status == "empty"


def test_pdf_text_and_image_only(tmp_path):
    ok = extract(Path("doc.pdf"), make_pdf(["The router password policy is documented here.", "Page two text."]))
    assert ok.status == "indexed" and len(ok.pages) == 2 and "router password policy" in ok.pages[0]
    scanned = extract(Path("scan.pdf"), make_pdf(["", ""]))
    assert scanned.status == "needs_ocr" and "OCR" in scanned.error
    broken = extract(Path("broken.pdf"), b"%PDF-1.4 nonsense")
    assert broken.status in ("error", "empty")


def test_chunking_respects_size_overlap_and_lines():
    paras = [f"Paragraph {i}: " + ("lorem ipsum dolor sit amet " * 8) for i in range(30)]
    text = "\n\n".join(paras)
    chunks = chunk_text(text, chunk_chars=600, overlap=120)
    assert len(chunks) > 5
    assert all(len(c.content) <= 600 + 1 for c in chunks)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    # line references point at the right place in the original text
    lines = text.split("\n")
    for c in chunks:
        assert lines[c.start_line - 1].strip() in c.content
    # every paragraph is present somewhere
    joined = "\n".join(c.content for c in chunks)
    assert all(p[:20] in joined for p in paras)


def test_chunking_splits_huge_lines_and_headings():
    chunks = chunk_text("x" * 5000, chunk_chars=1000, overlap=0)
    assert len(chunks) == 5
    md = "# A\ntext a\n# B\ntext b"
    assert len(chunk_text(md, chunk_chars=2000)) == 1
    assert chunk_text("", 100) == []


async def test_scanner_incremental_and_safe(tmp_path):
    root = tmp_path / "notes"
    (root / "sub").mkdir(parents=True)
    (root / "a.md").write_text("# Wifi\nThe guest wifi is called Orchard.")
    (root / "sub" / "b.txt").write_text("Backups run nightly at 02:00.")
    (root / ".env").write_text("SECRET=1")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.js").write_text("x")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "c.md").write_text("hidden")
    (root / "photo.jpg").write_bytes(b"\xff\xd8\xff")
    symlink_or_skip(tmp_path, root / "escape")
    files = [rel for _, rel in iter_files(root, ["node_modules"], [])]
    assert sorted(files) == ["a.md", "sub/b.txt"]

    emitted = []

    async def emit(rec):
        emitted.append(rec)

    payload = KnowledgeScanPayload(collection_id="c", root_path=str(root), exclude_dirs=["node_modules"])
    res = await scan(payload, PathPolicy([tmp_path]), emit)
    assert res.stats == {"indexed": 2} and sorted(res.seen) == ["a.md", "sub/b.txt"]
    known = {r.rel_path: KnownDocument(size=r.size, mtime=r.mtime, content_hash=r.content_hash) for r in emitted}
    emitted.clear()
    (root / "sub" / "b.txt").write_text("Backups run nightly at 03:00 now.")
    res2 = await scan(payload.model_copy(update={"known": known}), PathPolicy([tmp_path]), emit)
    assert res2.stats == {"unchanged": 1, "indexed": 1}
    # touch without change -> unchanged via hash
    a = root / "a.md"
    os.utime(a, (a.stat().st_atime, a.stat().st_mtime + 10))
    known2 = {r.rel_path: KnownDocument(size=r.size, mtime=r.mtime, content_hash=r.content_hash or "")
              for r in emitted}
    known2["a.md"] = known["a.md"]
    emitted.clear()
    res3 = await scan(payload.model_copy(update={"known": known2}), PathPolicy([tmp_path]), emit)
    assert res3.stats.get("unchanged") == 2


async def test_scanner_refuses_unauthorised_root(tmp_path):
    import pytest

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with pytest.raises(PermissionError):
        await scan(KnowledgeScanPayload(collection_id="c", root_path=str(tmp_path / "b")), PathPolicy([tmp_path / "a"]),
                   lambda r: None)


def test_vector_index_and_rrf():
    idx = VectorIndex(["a", "b", "c"], __import__("numpy").array([[1, 0], [0.9, 0.1], [0, 1]], dtype="float32"))
    hits = idx.search([1, 0], k=2)
    assert [h[0] for h in hits] == ["a", "b"]
    assert [h[0] for h in idx.search([1, 0], k=3, allowed={"c"})] == ["c"]
    fused = rrf([["a", "b"], ["b", "c"]])
    assert fused[0][0] == "b"
    import pytest

    with pytest.raises(ValueError, match="dimensions"):
        idx.search([1, 0, 0])
    assert VectorIndex([], None).search([1.0]) == []


def test_json_roundtrip_of_records():
    from homemesh.protocol.jobs import DocumentRecord

    rec = DocumentRecord(rel_path="a", size=1, mtime=1.0, status="indexed")
    assert DocumentRecord.model_validate(json.loads(rec.model_dump_json())) == rec


def test_scanner_skips_cloud_only_files_and_links_out_of_the_folder(tmp_path, monkeypatch):
    import sys

    from fsutil import symlink_or_skip
    from homemesh.knowledge import scanner
    from homemesh.protocol.jobs import KnowledgeScanPayload

    root = tmp_path / "docs"
    root.mkdir()
    (root / "local.md").write_text("# Local\n\nOn this computer.\n")
    (root / "cloud.md").write_text("placeholder")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("must not be indexed")
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(outside), str(root / "junction"))  # no admin rights needed
    else:
        symlink_or_skip(outside, root / "linked")
    files = [rel for _, rel in scanner.iter_files(root, [], [])]
    assert files == ["cloud.md", "local.md"]  # nothing reached through the junction/link

    class CloudStat:
        def __init__(self, st):
            self._st = st
            self.st_file_attributes = 0x00400000  # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

        def __getattr__(self, name):
            return getattr(self._st, name)

    cloud = root / "cloud.md"
    orig = type(cloud).stat
    monkeypatch.setattr(type(cloud), "stat", lambda self, **kw: CloudStat(orig(self, **kw))
                        if self.name == "cloud.md" else orig(self, **kw))
    read = []
    monkeypatch.setattr(type(cloud), "read_bytes", lambda self: read.append(self.name) or b"x")
    payload = KnowledgeScanPayload(collection_id="c", root_path=str(root), known={})
    rec = scanner.process_file(cloud, "cloud.md", payload)
    assert rec.status == "error" and "cloud-only" in rec.error and read == []
