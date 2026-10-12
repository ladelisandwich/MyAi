"""HTTP client the CLI uses to talk to a (local or remote) coordinator."""

from __future__ import annotations

import ssl
from typing import Any

import httpx

from homemesh.common.config import HomeMeshConfig
from homemesh.common.errors import HomeMeshError
from homemesh.common.paths import AppPaths


class NotRunning(HomeMeshError):
    code = "not_running"


class CoordinatorClient:
    def __init__(self, base_url: str, token: str | None, verify: ssl.SSLContext | bool = True,
                 timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(base_url=self.base_url, headers=headers, verify=verify, timeout=timeout,
                                    trust_env=False)

    @classmethod
    def local(cls, cfg: HomeMeshConfig, paths: AppPaths, url: str | None = None, token: str | None = None,
              ca_file: str | None = None) -> CoordinatorClient:
        base = url or cfg.local_base_url()
        if token is None and paths.cli_token_file.exists():
            token = paths.cli_token_file.read_text(encoding="utf-8").strip()
        verify: ssl.SSLContext | bool = True
        if base.startswith("https://"):
            ca = ca_file or (str(paths.tls_dir / "ca.pem") if (paths.tls_dir / "ca.pem").exists() else None)
            if ca is None and paths.secrets_dir.joinpath("coordinator-ca.pem").exists():
                ca = str(paths.secrets_dir / "coordinator-ca.pem")
            verify = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
        return cls(base, token, verify)

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self._client.request(method, path, **kw)
        except httpx.ConnectError as exc:
            raise NotRunning(f"Cannot reach the coordinator at {self.base_url}.",
                             hint="Start it with 'homemesh start' (or 'homemesh run' in the foreground).") from exc
        except httpx.TimeoutException as exc:
            raise NotRunning(f"The coordinator at {self.base_url} did not respond in time.") from exc
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
            except ValueError:
                err = {}
            msg = err.get("message") or f"HTTP {r.status_code}"
            hint = err.get("hint")
            if r.status_code == 401:
                hint = hint or ("The CLI token is missing or invalid. On the coordinator computer it is created "
                                "automatically after the owner account exists; elsewhere pass --token.")
            raise HomeMeshError(msg, code=err.get("code", f"http_{r.status_code}"), hint=hint,
                                status_code=r.status_code)
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r.text

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None) -> Any:
        return self.request("POST", path, json=body if body is not None else {})

    def patch(self, path: str, body: Any) -> Any:
        return self.request("PATCH", path, json=body)

    def put(self, path: str, body: Any) -> Any:
        return self.request("PUT", path, json=body)
