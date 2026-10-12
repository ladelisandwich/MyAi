"""Build tiny valid PDFs for tests (no external PDF writer needed)."""


def make_pdf(pages: list[str]) -> bytes:
    objects: list[bytes] = []
    n_pages = len(pages)
    page_ids = [3 + i * 2 for i in range(n_pages)]
    font_id = 3 + n_pages * 2
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {n_pages} >>".encode())
    for i, text in enumerate(pages):
        content_id = page_ids[i] + 1
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
                       f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode())
        ops = "BT /F1 12 Tf 72 720 Td " + " ".join(
            f"({line.replace('(', '').replace(')', '')}) Tj 0 -16 Td" for line in text.split("\n") if line) + " ET"
        stream = ops.encode() if text else b""
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
