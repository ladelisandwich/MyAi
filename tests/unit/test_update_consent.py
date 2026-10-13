"""Consent to remote updates is a marker file, read live; nothing else can turn it on."""

import os
import stat
from pathlib import Path

import pytest

from homemesh.common.config import HomeMeshConfig, load_config
from homemesh.protocol import messages as pm
from homemesh.update import consent

ROOT = Path(__file__).resolve().parents[2]
MARKER = "remote-updates.allowed"


def test_allow_and_disallow_take_effect_immediately(home):
    assert consent.remote_allowed(home) is False
    path = consent.allow(home)
    assert path == consent.marker_path(home) and path.parent == home.config_dir and path.name == MARKER
    assert consent.remote_allowed(home) is True
    text = path.read_text(encoding="utf-8")
    assert text.startswith("allowed at ") and " by " in text and text.endswith("\n")
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    consent.allow(home)  # idempotent
    assert consent.disallow(home) is True
    assert consent.remote_allowed(home) is False
    assert consent.disallow(home) is False


def test_a_directory_or_dangling_link_is_not_consent(home, tmp_path):
    consent.marker_path(home).mkdir()
    assert consent.remote_allowed(home) is False
    consent.marker_path(home).rmdir()
    try:
        os.symlink(tmp_path / "missing", consent.marker_path(home))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    assert consent.remote_allowed(home) is False
    assert consent.disallow(home) is True and not os.path.lexists(consent.marker_path(home))


def test_environment_and_config_cannot_turn_it_on(home, monkeypatch):
    for key in ("HOMEMESH__UPDATE__ALLOW_REMOTE", "HOMEMESH__UPDATE__ALLOWED", "HOMEMESH__UPDATE__REMOTE_ALLOWED",
                "HOMEMESH_UPDATE_ALLOW", "HOMEMESH_REMOTE_UPDATES"):
        monkeypatch.setenv(key, "true")
    assert consent.remote_allowed(home) is False
    assert "update" not in HomeMeshConfig.model_fields
    with pytest.raises(Exception):  # noqa: B017 - the config refuses the unknown section outright
        load_config(home, environ={"HOMEMESH__UPDATE__ALLOW_REMOTE": "true"})
    assert consent.remote_allowed(home) is False


def test_no_protocol_message_from_the_hub_carries_update_settings():
    for model in (pm.Welcome, pm.NodeConfig):
        assert not [f for f in model.model_fields if "update" in f or "build" in f or "allow" in f], model


def test_only_consent_py_knows_the_marker_name():
    hits = []
    for base in (ROOT / "src", ROOT / "apps" / "web" / "src", ROOT / "deploy", ROOT / "scripts"):
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if not f.is_file() or f.suffix in (".pyc",) or "node_modules" in f.parts or "web_dist" in f.parts:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if MARKER in text:
                hits.append(f.relative_to(ROOT).as_posix())
    assert hits == ["src/homemesh/update/consent.py"]
