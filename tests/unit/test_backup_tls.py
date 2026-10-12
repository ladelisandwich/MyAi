import socket
import sqlite3
import ssl
import threading

import pytest

from homemesh.coordinator.backup import (
    BackupError,
    create_backup,
    decrypt_file,
    encrypt_file,
    restore_backup,
    verify_backup,
)
from homemesh.coordinator.db.migrate import migrate
from homemesh.coordinator.tls import ca_fingerprint, ensure_tls
from homemesh.worker.enroll import pem_fingerprint


def _seed(paths, text="hello"):
    paths.ensure()
    c = sqlite3.connect(paths.db_file, isolation_level=None)
    migrate(c)
    c.execute("INSERT INTO users(id, username, password_hash, created_at, updated_at) VALUES ('u','o','h','t','t')")
    c.execute("INSERT INTO conversations(id, user_id, title, created_at, updated_at) VALUES ('c','u','t','t','t')")
    c.execute("INSERT INTO messages(id, conversation_id, role, content, created_at, updated_at) VALUES ('m','c','user',?,'t','t')", (text,))
    c.close()
    paths.config_file.write_text("[node]\nname = \"orig\"\n")
    (paths.attachments_dir / "a.png").write_bytes(b"\x89PNG\r\n\x1a\nimg")


def _messages(paths):
    with sqlite3.connect(paths.db_file) as c:
        return [r[0] for r in c.execute("SELECT content FROM messages")]


def test_backup_restore_round_trip(home):
    _seed(home, "original message")
    info = create_backup(home)
    assert set(info.manifest["includes"]) >= {"database", "config", "attachments"}
    assert verify_backup(info.path)["schema_version"] >= 1
    # change data after the backup
    with sqlite3.connect(home.db_file) as c:
        c.execute("UPDATE messages SET content='changed'")
    home.config_file.write_text("[node]\nname = \"changed\"\n")
    res = restore_backup(info.path, home)
    assert _messages(home) == ["original message"]
    assert 'name = "orig"' in home.config_file.read_text()
    assert (home.attachments_dir / "a.png").exists()
    # nothing was deleted: the replaced data was moved aside
    from pathlib import Path

    aside = Path(res["previous_data_moved_to"])
    with sqlite3.connect(aside / "homemesh.sqlite3") as c:
        assert c.execute("SELECT content FROM messages").fetchone()[0] == "changed"


def test_encrypted_backup_requires_correct_passphrase(home):
    _seed(home)
    info = create_backup(home, passphrase="hunter2-but-longer")
    assert info.path.name.endswith(".enc")
    assert b"original" not in info.path.read_bytes()
    with pytest.raises(BackupError, match="passphrase"):
        verify_backup(info.path)
    with pytest.raises(BackupError, match="Wrong passphrase"):
        verify_backup(info.path, "wrong passphrase")
    assert verify_backup(info.path, "hunter2-but-longer")["encrypted"] is True


def test_tampered_or_truncated_archives_detected(tmp_path, home):
    _seed(home)
    plain = create_backup(home).path
    data = bytearray(plain.read_bytes())
    data[len(data) // 2] ^= 0xFF
    bad = tmp_path / "bad.hmbackup"
    bad.write_bytes(bytes(data))
    with pytest.raises(BackupError):
        verify_backup(bad)
    src = tmp_path / "x.bin"
    src.write_bytes(b"A" * (3 * 1024 * 1024 + 7))
    enc = tmp_path / "x.enc"
    encrypt_file(src, enc, "pw-pw-pw-pw")
    out = tmp_path / "x.out"
    decrypt_file(enc, out, "pw-pw-pw-pw")
    assert out.read_bytes() == src.read_bytes()
    raw = enc.read_bytes()
    trunc = tmp_path / "trunc.enc"
    trunc.write_bytes(raw[: len(raw) - (1024 * 1024 + 20)])  # drop the final chunk(s)
    with pytest.raises(BackupError):
        decrypt_file(trunc, tmp_path / "t.out", "pw-pw-pw-pw")


def test_tls_ca_pinning_and_hostname_verification(tmp_path):
    mat = ensure_tls(tmp_path / "tls")
    fp = ca_fingerprint(mat.ca_cert)
    assert pem_fingerprint(mat.ca_cert.read_text()) == fp
    # Re-issuing the server certificate keeps the CA (and every worker's pin).
    (tmp_path / "tls" / "server.pem").unlink()
    mat2 = ensure_tls(tmp_path / "tls", ["homemesh.lan"])
    assert ca_fingerprint(mat2.ca_cert) == fp and "homemesh.lan" in mat2.sans

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(mat2.cert, mat2.key)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        try:
            with server_ctx.wrap_socket(conn, server_side=True) as s:
                s.recv(10)
        except (ssl.SSLError, OSError):
            pass

    for _ in range(2):
        threading.Thread(target=serve, daemon=True).start()
    # A strict client (Python's default context incl. VERIFY_X509_STRICT) trusting only the pinned CA succeeds:
    ok_ctx = ssl.create_default_context(cafile=str(mat2.ca_cert))
    with socket.create_connection(("127.0.0.1", port)) as raw, ok_ctx.wrap_socket(raw, server_hostname="127.0.0.1") as s:
        s.sendall(b"ping")
    # ...and a client trusting a different CA refuses.
    other = ensure_tls(tmp_path / "other")
    bad_ctx = ssl.create_default_context(cafile=str(other.ca_cert))
    with pytest.raises(ssl.SSLCertVerificationError):
        with socket.create_connection(("127.0.0.1", port)) as raw, bad_ctx.wrap_socket(raw, server_hostname="127.0.0.1"):
            pass
    srv.close()
