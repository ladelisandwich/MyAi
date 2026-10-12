"""Worker credential storage (``<data>/secrets/worker-credentials.json``, mode 0600).

Contents: coordinator URL, node id, node secret and - for TLS coordinators - the path and
fingerprint of the pinned coordinator CA certificate.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from homemesh.common.errors import ConfigError
from homemesh.common.paths import write_private_file


@dataclass
class WorkerCredentials:
    coordinator_url: str
    node_id: str
    node_secret: str
    ca_file: str = ""
    ca_fingerprint: str = ""
    embedded: bool = False
    enrolled_at: str = ""

    def save(self, path: Path) -> None:
        write_private_file(path, json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: Path) -> WorkerCredentials:
        if not path.exists():
            raise ConfigError(
                f"This computer is not joined to a HomeMesh coordinator ({path} is missing).",
                hint="Run 'homemesh worker join --coordinator <url> --code <pairing code>'.",
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ConfigError(f"Worker credentials file {path} is corrupt: {exc}",
                              hint="Re-join the coordinator with 'homemesh worker join'.") from exc
