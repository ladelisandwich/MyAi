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
