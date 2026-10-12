"""Text extraction adapters.

Each adapter turns a file into plain text (or per-page text for PDFs). Adapters never
invent content: when a file cannot be read as text the result says so (``unsupported``,
``needs_ocr``, ``error``). Add new formats by registering an adapter in ``ADAPTERS``.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

TEXT_EXTS = {".txt", ".md", ".markdown", ".rst", ".log", ".org", ".adoc", ".tex", ".text"}
CODE_EXTS = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs",
    ".go", ".rs", ".rb", ".php", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1", ".bat", ".cmd", ".sql", ".toml",
    ".yaml", ".yml", ".ini", ".cfg", ".conf", ".html", ".htm", ".css", ".scss", ".less", ".xml", ".swift", ".kt",
    ".kts", ".lua", ".r", ".scala", ".vue", ".svelte", ".dart", ".pl", ".ex", ".exs", ".erl", ".hs", ".clj",
    ".gradle", ".cmake", ".nix", ".tf", ".proto", ".graphql", ".ipynb", ".service", ".container",
}
CODE_FILENAMES = {"Dockerfile", "Containerfile", "Makefile", "CMakeLists.txt", "Justfile", "Vagrantfile",
                  "Gemfile", "Rakefile", "Procfile"}
MIN_PDF_CHARS_PER_PAGE = 25


@dataclass
class Extracted:
    status: str  # indexed | needs_ocr | unsupported | error | empty
    mime: str
    text: str = ""
    pages: list[str] = field(default_factory=list)  # PDFs: one entry per page
    error: str | None = None
    csv_header: str | None = None


def _decode(data: bytes) -> str | None:
    if b"\x00" in data[:8192]:
        return None
    for enc in ("utf-8", "utf-8-sig"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return None


def _text_adapter(mime: str) -> Callable[[Path, bytes], Extracted]:
    def adapter(path: Path, data: bytes) -> Extracted:
        text = _decode(data)
        if text is None:
            return Extracted("unsupported", mime, error="file is binary or not valid text")
        if not text.strip():
            return Extracted("empty", mime)
        return Extracted("indexed", mime, text=text)

    return adapter


def _json_adapter(path: Path, data: bytes) -> Extracted:
    text = _decode(data)
    if text is None:
        return Extracted("unsupported", "application/json", error="not valid text")
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        # Still index the raw text, but record that it is not valid JSON.
        return Extracted("indexed", "application/json", text=text, error=f"invalid JSON: {exc.msg}")
    return Extracted("indexed", "application/json", text=text) if text.strip() else Extracted("empty", "application/json")


def _csv_adapter(path: Path, data: bytes) -> Extracted:
    text = _decode(data)
    if text is None:
        return Extracted("unsupported", "text/csv", error="not valid text")
    if not text.strip():
        return Extracted("empty", "text/csv")
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        pass
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    header = rows[0] if rows else []
    lines = []
    for row in rows[1:]:
        if header and len(row) == len(header):
            lines.append(" | ".join(f"{h}: {v}" for h, v in zip(header, row, strict=True)))
        else:
            lines.append(" | ".join(row))
    return Extracted("indexed", "text/csv", text="\n".join(lines) or text, csv_header=" | ".join(header))


def _pdf_adapter(path: Path, data: bytes) -> Extracted:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        return Extracted("unsupported", "application/pdf", error="PDF support requires the 'pypdf' package")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                return Extracted("error", "application/pdf", error="PDF is password-protected")
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:
                pages.append("")
                _ = exc
    except (PdfReadError, ValueError, KeyError, OSError) as exc:
        return Extracted("error", "application/pdf", error=f"could not read PDF: {exc}")
    total = sum(len(p.strip()) for p in pages)
    if not pages:
        return Extracted("empty", "application/pdf")
    if total < MIN_PDF_CHARS_PER_PAGE * len(pages):
        return Extracted("needs_ocr", "application/pdf", pages=pages,
                         error=f"PDF has little or no extractable text ({total} characters over {len(pages)} pages); "
                               "it is probably scanned images and needs OCR, which is not available yet.")
    return Extracted("indexed", "application/pdf", pages=pages)


ADAPTERS: dict[str, Callable[[Path, bytes], Extracted]] = {}
for _ext in TEXT_EXTS:
    ADAPTERS[_ext] = _text_adapter("text/markdown" if _ext in (".md", ".markdown") else "text/plain")
for _ext in CODE_EXTS:
    ADAPTERS[_ext] = _text_adapter("text/x-code")
ADAPTERS[".json"] = _json_adapter
ADAPTERS[".jsonl"] = _text_adapter("application/jsonl")
ADAPTERS[".csv"] = _csv_adapter
ADAPTERS[".tsv"] = _csv_adapter
ADAPTERS[".pdf"] = _pdf_adapter


def adapter_for(path: Path) -> Callable[[Path, bytes], Extracted] | None:
    if path.name in CODE_FILENAMES:
        return _text_adapter("text/x-code")
    return ADAPTERS.get(path.suffix.lower())


def supported(path: Path) -> bool:
    return adapter_for(path) is not None


def extract(path: Path, data: bytes) -> Extracted:
    adapter = adapter_for(path)
    if adapter is None:
        return Extracted("unsupported", "application/octet-stream", error=f"no extractor for '{path.suffix}' files")
    try:
        return adapter(path, data)
    except Exception as exc:
        return Extracted("error", "application/octet-stream", error=f"extraction failed: {exc}")
