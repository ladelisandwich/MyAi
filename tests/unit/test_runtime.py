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
