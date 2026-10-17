"""A PEP 517 build backend with no build requirements, for the stand-in HomeMesh of ``test_update_e2e.py``.

It is committed into the test repository as ``_build/standin_backend.py`` (``backend-path``), so the update
runner's real ``pip install <build tree>`` works without a package index: pip installs nothing into the
isolated build environment and this backend zips ``src/`` into a wheel with the standard library.
"""

import base64
import hashlib
import os
import zipfile

NAME = "homemesh_standin"
VERSION = "0.0.1"


def _entries() -> list[tuple[str, bytes]]:
    src = os.path.join(os.getcwd(), "src")  # pip runs the hooks in the source tree
    out = []
    for dirpath, dirnames, names in os.walk(src):
        dirnames.sort()
        for name in sorted(names):
            full = os.path.join(dirpath, name)
            with open(full, "rb") as fh:
                out.append((os.path.relpath(full, src).replace(os.sep, "/"), fh.read()))
    return out


def _hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):  # PEP 517 hook
    dist = f"{NAME}-{VERSION}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: homemesh-standin\nVersion: {VERSION}\n".encode()
    wheel = b"Wheel-Version: 1.0\nGenerator: homemesh-standin\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    entries = [*_entries(), (f"{dist}/METADATA", metadata), (f"{dist}/WHEEL", wheel)]
    record = [*(f"{arc},{_hash(data)},{len(data)}" for arc, data in entries), f"{dist}/RECORD,,"]
    name = f"{NAME}-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(os.path.join(wheel_directory, name), "w", zipfile.ZIP_DEFLATED) as zf:
        for arc, data in entries:
            zf.writestr(arc, data)
        zf.writestr(f"{dist}/RECORD", "\n".join(record) + "\n")
    return name
