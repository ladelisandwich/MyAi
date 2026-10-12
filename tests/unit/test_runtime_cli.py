import asyncio

from homemesh.common.paths import AppPaths
from homemesh.runtime import _watch_stop_request


async def test_stop_request_file_triggers_graceful_stop(tmp_path):
    paths = AppPaths(config_dir=tmp_path, data_dir=tmp_path / "data")
    paths.data_dir.mkdir()
    paths.stop_request_file.write_text("stale")  # left over from an earlier run: must be ignored
    stop = asyncio.Event()
    task = asyncio.create_task(_watch_stop_request(paths, stop, interval_s=0.01))
    await asyncio.sleep(0.05)
    assert not stop.is_set() and not paths.stop_request_file.exists()
    paths.stop_request_file.write_text("123")
    await asyncio.wait_for(task, 2)
    assert stop.is_set() and not paths.stop_request_file.exists()


def test_worker_link_reports_protocol_mismatch(tmp_path, monkeypatch):
    import httpx

    from homemesh.cli import doctor
    from homemesh.common.config import HomeMeshConfig
    from homemesh.worker.credentials import WorkerCredentials

    paths = AppPaths(config_dir=tmp_path, data_dir=tmp_path / "data")
    monkeypatch.setattr(WorkerCredentials, "load", classmethod(
        lambda cls, p: cls(coordinator_url="http://127.0.0.1:1", node_id="node_x", node_secret="s")))

    def fake_get(url, **kw):
        return httpx.Response(200, json={"version": "9.0.0", "protocol_version": 99})

    monkeypatch.setattr(doctor.httpx, "get", fake_get)
    check = doctor._worker_link(HomeMeshConfig(), paths)
    assert check.status == doctor.FAIL and "protocol v99" in check.detail and "Upgrade" in check.hint


def test_cli_output_survives_legacy_console_encoding(tmp_path):
    """Windows consoles redirected to a pipe use cp1252, which has no ✓/✗; output must not crash."""
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "HOMEMESH_HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, "-m", "homemesh", "config", "validate"], capture_output=True, env=env,
                       timeout=60, check=False)
    assert r.returncode == 0, r.stderr.decode("cp1252", "replace")
    assert r.stdout.decode("cp1252").startswith("[ok] ")


def test_tls_init_new_ca_replaces_ca_and_keeps_old(tmp_path):
    from homemesh.cli.main import main
    from homemesh.coordinator.tls import ca_fingerprint

    assert main(["--home", str(tmp_path), "tls", "init"]) == 0
    first = ca_fingerprint(tmp_path / "data" / "tls" / "ca.pem")
    assert main(["--home", str(tmp_path), "tls", "init"]) == 0  # idempotent: same CA
    assert ca_fingerprint(tmp_path / "data" / "tls" / "ca.pem") == first
    assert main(["--home", str(tmp_path), "tls", "init", "--new-ca"]) == 0
    assert ca_fingerprint(tmp_path / "data" / "tls" / "ca.pem") != first
    kept = list((tmp_path / "data").glob("tls-replaced-*"))
    assert len(kept) == 1 and ca_fingerprint(kept[0] / "ca.pem") == first
