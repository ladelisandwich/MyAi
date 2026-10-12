"""Joining a worker to a coordinator (pairing code + TLS certificate pinning).

1. If the coordinator uses HTTPS with the HomeMesh local CA, download the CA certificate
   over a connection that is *not yet* verified, compute its SHA-256 fingerprint and compare
   it with the fingerprint the owner copied from the coordinator. Any mismatch aborts.
   This is the standard trust-on-first-use with an out-of-band check (like SSH host keys);
   after this point every connection is fully verified against the pinned CA.
2. POST the one-time pairing code to ``/api/v1/nodes/enroll`` over the verified connection.
3. Store the returned node id + secret (0600) and the pinned CA.

Plain HTTP is only accepted for a coordinator on this same machine (loopback) unless the
owner explicitly passes ``--allow-insecure-http``.
"""

from __future__ import annotations

import socket
import ssl
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from homemesh.common.config import is_loopback_host
from homemesh.common.errors import AuthError, ConfigError, HomeMeshError
from homemesh.common.paths import AppPaths, write_private_file
from homemesh.common.security import fingerprint_sha256, normalize_fingerprint
from homemesh.common.timeutil import now_iso
from homemesh.worker.credentials import WorkerCredentials


class EnrollmentError(HomeMeshError):
    code = "enrollment_failed"


def fetch_ca_unverified(url: str, timeout: float = 10.0) -> str:
    """Download the coordinator's CA PEM without verification (it is verified by fingerprint next)."""
    parts = urlsplit(url)
    host, port = parts.hostname or "", parts.port or 443
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # deliberately unverified: the fingerprint check below is the trust anchor
    request = (f"GET /api/v1/tls/ca HTTP/1.1\r\nHost: {host}\r\nUser-Agent: homemesh-enroll\r\n"
               "Connection: close\r\n\r\n").encode()
    with socket.create_connection((host, port), timeout=timeout) as raw, ctx.wrap_socket(raw, server_hostname=host) as s:
        s.sendall(request)
        chunks = []
        while True:
            data = s.recv(65536)
            if not data:
                break
            chunks.append(data)
    response = b"".join(chunks).decode("latin-1")
    head, _, body = response.partition("\r\n\r\n")
    status_line = head.split("\r\n", 1)[0]
    if " 200 " not in status_line + " ":
        raise EnrollmentError(f"Coordinator did not provide its CA certificate ({status_line}).",
                              hint="Is the coordinator using tls_mode=self_signed? For a reverse proxy with a "
                                   "public certificate, join with --system-trust instead.")
    if "transfer-encoding: chunked" in head.lower():
        body = _dechunk(body)
    if "BEGIN CERTIFICATE" not in body:
        raise EnrollmentError("The coordinator's CA response did not contain a certificate.")
    return body.strip() + "\n"


def _dechunk(body: str) -> str:
    out, rest = [], body
    while rest:
        size_line, _, rest = rest.partition("\r\n")
        size = int(size_line.split(";")[0] or "0", 16)
        if size == 0:
            break
        out.append(rest[:size])
        rest = rest[size + 2:]
    return "".join(out)


def pem_fingerprint(pem: str) -> str:
    der = ssl.PEM_cert_to_DER_cert(pem)
    return fingerprint_sha256(der)


def join(coordinator_url: str, code: str, paths: AppPaths, *, node_name: str, fingerprint: str | None = None,
         system_trust: bool = False, allow_insecure_http: bool = False, timeout: float = 15.0) -> WorkerCredentials:
    url = coordinator_url.rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"Invalid coordinator URL {coordinator_url!r}; expected e.g. https://192.168.1.10:8765")
    ca_file: str = ""
    ca_fp = ""
    verify: ssl.SSLContext | bool = True
    if parts.scheme == "http":
        if not is_loopback_host(parts.hostname) and not allow_insecure_http:
            raise EnrollmentError(
                "Refusing to join over unencrypted HTTP: the pairing code and node secret would be visible on "
                "your network.", hint="Enable TLS on the coordinator ('homemesh tls init' then restart) and use "
                                      "https://, or pass --allow-insecure-http if you accept the risk.")
    elif system_trust:
        verify = ssl.create_default_context()
    else:
        if not fingerprint:
            raise EnrollmentError("A TLS fingerprint is required to join an HTTPS coordinator safely.",
                                  hint="Copy it from the coordinator's 'Add a computer' dialog or 'homemesh tls show', "
                                       "and pass --fingerprint.")
        pem = fetch_ca_unverified(url, timeout)
        actual = pem_fingerprint(pem)
        if normalize_fingerprint(actual) != normalize_fingerprint(fingerprint):
            raise EnrollmentError(
                "TLS fingerprint mismatch! The coordinator presented a different certificate than expected.\n"
                f"  expected: {normalize_fingerprint(fingerprint)}\n  received: {actual}",
                hint="Do not continue if you are unsure: this could mean another device is impersonating your "
                     "coordinator. Re-check the fingerprint shown on the coordinator.")
        ca_path = paths.secrets_dir / "coordinator-ca.pem"
        write_private_file(ca_path, pem)
        ca_file, ca_fp = str(ca_path), actual
        verify = ssl.create_default_context(cafile=ca_file)
    try:
        with httpx.Client(verify=verify, timeout=timeout, trust_env=False) as client:
            r = client.post(f"{url}/api/v1/nodes/enroll", json={"code": code, "name": node_name})
    except httpx.ConnectError as exc:
        raise EnrollmentError(f"Cannot connect to the coordinator at {url}: {exc}",
                              hint="Check the address, that the coordinator is running and reachable from this "
                                   "computer (firewall), and that both are on the same network/VPN.") from exc
    if r.status_code != 200:
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        if r.status_code in (401, 403):
            raise AuthError(err.get("message", "Pairing code rejected."), code="pairing_invalid", hint=err.get("hint"))
        raise EnrollmentError(err.get("message", f"Enrollment failed (HTTP {r.status_code})."), hint=err.get("hint"))
    data = r.json()
    creds = WorkerCredentials(coordinator_url=url, node_id=data["node_id"], node_secret=data["node_secret"],
                              ca_file=ca_file, ca_fingerprint=ca_fp, enrolled_at=now_iso())
    creds.save(paths.worker_credentials_file)
    return creds


def leave(paths: AppPaths) -> bool:
    removed = False
    for f in (paths.worker_credentials_file, paths.secrets_dir / "coordinator-ca.pem"):
        if Path(f).exists():
            Path(f).unlink()
            removed = True
    return removed
