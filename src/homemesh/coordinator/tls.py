"""Local TLS: a private HomeMesh CA plus a server certificate signed by it.

Trust model (certificate pinning): when a worker joins, the owner gives it the CA's SHA-256
fingerprint (printed next to the pairing code). The worker downloads the CA certificate,
refuses to continue unless the fingerprint matches exactly, stores it, and from then on
verifies every TLS connection against that CA with full hostname checking. TLS verification
is never disabled. Browsers will warn about the private CA until it is imported (see
SECURITY.md) - or put HomeMesh behind a reverse proxy with a publicly trusted certificate
(``tls_mode = "proxy"``).

The server certificate is re-issued automatically when its host names/IPs change or it is
within 30 days of expiry; the CA (and therefore every worker's pin) stays the same.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path

import psutil
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from homemesh.common.paths import ensure_private_dir, write_private_file
from homemesh.common.security import fingerprint_sha256

CA_DAYS = 3650
LEAF_DAYS = 397


@dataclass
class TlsMaterial:
    ca_cert: Path
    cert: Path
    key: Path
    ca_fingerprint: str
    sans: list[str]


def local_addresses() -> list[str]:
    out = {"127.0.0.1", "::1"}
    try:
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family in (socket.AF_INET, socket.AF_INET6):
                    ip = a.address.split("%")[0]
                    try:
                        obj = ipaddress.ip_address(ip)
                    except ValueError:
                        continue
                    if not obj.is_link_local and not obj.is_multicast:
                        out.add(str(obj))
    except OSError:
        pass
    return sorted(out)


def default_names() -> list[str]:
    host = socket.gethostname() or "homemesh"
    names = {"localhost", host, f"{host}.local"}
    try:
        fq = socket.getfqdn()
        if fq and "." in fq:
            names.add(fq)
    except OSError:
        pass
    return sorted(names)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _write_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    write_private_file(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))


def _load_key(path: Path) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    return key


def _create_ca(tls_dir: Path) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HomeMesh AI"),
                      x509.NameAttribute(NameOID.COMMON_NAME, f"HomeMesh Local CA ({socket.gethostname()})")])
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(minutes=5)).not_valid_after(_now() + dt.timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(ski, critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski), critical=False)
            .sign(key, hashes.SHA256()))
    _write_key(tls_dir / "ca.key", key)
    (tls_dir / "ca.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert, key


def _create_leaf(tls_dir: Path, ca: x509.Certificate, ca_key: ec.EllipticCurvePrivateKey, sans: list[str]) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    alt: list[x509.GeneralName] = []
    for s in sans:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(s)))
        except ValueError:
            alt.append(x509.DNSName(s))
    cn = next((s for s in sans if not s[0].isdigit() and s != "localhost"), "homemesh")
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HomeMesh AI"),
                                     x509.NameAttribute(NameOID.COMMON_NAME, cn[:64])]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(minutes=5)).not_valid_after(_now() + dt.timedelta(days=LEAF_DAYS))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    _write_key(tls_dir / "server.key", key)
    (tls_dir / "server.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM)
                                         + ca.public_bytes(serialization.Encoding.PEM))


def _leaf_sans(path: Path) -> tuple[set[str], dt.datetime] | None:
    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError):
        return None
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set(), cert.not_valid_after_utc
    names = {str(v) for v in ext.get_values_for_type(x509.IPAddress)} | set(ext.get_values_for_type(x509.DNSName))
    return names, cert.not_valid_after_utc


def ca_fingerprint(ca_pem: Path) -> str:
    cert = x509.load_pem_x509_certificate(ca_pem.read_bytes())
    return fingerprint_sha256(cert.public_bytes(serialization.Encoding.DER))


def ensure_tls(tls_dir: Path, extra_sans: list[str] | None = None) -> TlsMaterial:
    ensure_private_dir(tls_dir)
    ca_path, ca_key_path = tls_dir / "ca.pem", tls_dir / "ca.key"
    if ca_path.exists() and ca_key_path.exists():
        ca = x509.load_pem_x509_certificate(ca_path.read_bytes())
        ca_key = _load_key(ca_key_path)
    else:
        ca, ca_key = _create_ca(tls_dir)
    sans = sorted(set(default_names()) | set(local_addresses()) | set(extra_sans or []))
    leaf = _leaf_sans(tls_dir / "server.pem")
    if (leaf is None or not (tls_dir / "server.key").exists() or not set(sans) <= leaf[0]
            or leaf[1] - _now() < dt.timedelta(days=30)):
        _create_leaf(tls_dir, ca, ca_key, sans)
    return TlsMaterial(ca_cert=ca_path, cert=tls_dir / "server.pem", key=tls_dir / "server.key",
                       ca_fingerprint=ca_fingerprint(ca_path), sans=sans)
