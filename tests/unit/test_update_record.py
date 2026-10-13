"""Install record and build identity: never fabricated, unknown with a reason."""

import json
import os
import shutil
import subprocess
import sys

import pytest

from fsutil import symlink_or_skip
from homemesh.update import consent, record
from homemesh.version import __version__

SHA = "b" * 40


def _install(tmp_path, *, commit=SHA, source_dir=None, built_by="installer"):
    """A blue/green layout: <prefix>/venvs/<id>/ with a record, and the stable path <prefix>/venv."""
    prefix = tmp_path / "prefix"
    venv = prefix / "venvs" / "bbbbbbbbbbbb-20261013120000"
    venv.mkdir(parents=True)
    rec = record.make_record(venv=venv, stable_venv=prefix / "venv", built_by=built_by, source_dir=source_dir,
                             commit=commit, commit_date="2026-10-13T10:00:00+02:00", dirty=False, branch="main",
                             remote="origin", git="/usr/bin/git", built_at="2026-10-13T12:00:00.000Z")
    record.write(rec)
    return prefix, venv, rec


def test_record_round_trip_and_tolerant_load(tmp_path):
    prefix, venv, rec = _install(tmp_path, source_dir=tmp_path / "src")
    loaded = record.load(venv)
    assert loaded == rec and loaded.layout == record.layout_version() == 1
    assert loaded.venv == os.path.abspath(venv) and loaded.stable_venv == os.path.abspath(prefix / "venv")
    assert loaded.python == ".".join(map(str, sys.version_info[:3]))
    data = json.loads(record.record_path(venv).read_text(encoding="utf-8"))
    assert data["v"] == 1 and "url" not in json.dumps(data).lower()
    data["added_by_a_later_version"] = {"x": 1}
    record.record_path(venv).write_text(json.dumps(data), encoding="utf-8")
    assert record.load(venv) == rec  # unknown fields ignored
    record.record_path(venv).write_text(json.dumps({**data, "commit": "HEAD"}), encoding="utf-8")
    assert record.load(venv) is None
    record.record_path(venv).write_text("{not json", encoding="utf-8")
    assert record.load(venv) is None
    assert record.load(tmp_path / "nowhere") is None


def test_make_record_drops_ref_names_the_runner_would_refuse(tmp_path):
    rec = record.make_record(venv=tmp_path, stable_venv=tmp_path, built_by="updater", branch="--x", remote="a b",
                             run_id="u20261013120000-0123abcd")
    assert rec.branch is None and rec.remote is None and rec.run_id == "u20261013120000-0123abcd"


def test_build_info_from_record(tmp_path, home):
    src = tmp_path / "src"
    (src / ".git").mkdir(parents=True)
    _prefix, venv, rec = _install(tmp_path, source_dir=src)
    # stable_venv must resolve to the running prefix; point the record at the venv itself (no link needed)
    rec = rec.model_copy(update={"stable_venv": str(venv)})
    info = record.build_info(home, prefix=venv, record=rec)
    assert info.source == "installer" and info.commit == SHA and info.branch == "main" and info.dirty is False
    assert info.version == __version__ and info.installed_at == "2026-10-13T12:00:00.000Z"
    assert info.can_update is (record.current_os() != "other") and info.reason is None
    assert info.source_dir == os.path.abspath(src) and info.logs_dir == str(home.logs_dir)
    assert info.update_allowed is False
    consent.allow(home)
    assert record.build_info(home, prefix=venv, record=rec).update_allowed is True
    shutil.rmtree(src / ".git")
    info = record.build_info(home, prefix=venv, record=rec)
    assert not info.can_update and "not a git checkout" in info.cannot_update_reason


def test_build_info_through_the_stable_link(tmp_path, home):
    src = tmp_path / "src"
    (src / ".git").mkdir(parents=True)
    prefix, venv, _rec = _install(tmp_path, source_dir=src, built_by="updater")
    symlink_or_skip(venv, prefix / "venv")
    info = record.build_info(home, prefix=prefix / "venv")  # sys.prefix is the link path; record read via it
    assert info.source == "updater" and info.commit == SHA
    other = prefix / "venvs" / "cccccccccccc-20261013130000"
    shutil.copytree(venv, other)
    info = record.build_info(home, prefix=other)  # a venv the link doesn't point at
    assert info.source == "unknown" and info.commit is None and not info.can_update and "not from" in info.reason


def test_build_info_without_git_checkout_at_install(tmp_path, home):
    _prefix, venv, rec = _install(tmp_path, commit=None, source_dir=None)
    rec = rec.model_copy(update={"stable_venv": str(venv)})
    info = record.build_info(home, prefix=venv, record=rec)
    assert info.source == "installer" and info.commit is None and not info.can_update
    assert "not a git checkout" in info.reason and record.format_version_line(info).startswith(
        f"HomeMesh {__version__} (commit unknown: it was installed from a folder that is not a git checkout")


def test_build_info_dev_and_unknown(tmp_path, home):
    checkout = tmp_path / "MyAi"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    pkg = checkout / "src" / "homemesh"
    pkg.mkdir(parents=True)
    venv = tmp_path / "venv"
    venv.mkdir()
    info = record.build_info(home, prefix=venv, package_dir=pkg)
    assert info.source == "dev" and info.commit is None and not info.can_update
    assert info.reason == record.DEV_REASON and info.source_dir == str(checkout)
    assert record.format_version_line(info) == f"HomeMesh {__version__} (commit unknown: editable/development install)"

    site = tmp_path / "venv" / "lib" / "site-packages" / "homemesh"
    site.mkdir(parents=True)
    info = record.build_info(home, prefix=venv, package_dir=site)
    assert info.source == "unknown" and "without an install record" in info.reason and not info.can_update
    record.record_path(venv).write_text("garbage", encoding="utf-8")
    info = record.build_info(home, prefix=venv, package_dir=pkg)
    assert info.source == "unknown" and "unreadable" in info.reason  # a broken record is not a dev install


def test_build_info_of_this_test_run_is_honest(home):
    info = record.build_info(home)
    assert info.version == __version__
    if info.source in ("dev", "unknown"):
        assert info.commit is None and info.reason and not info.can_update
    assert info.to_wire()["v"] == 1


def test_version_line_with_commit(home):
    info = record.build_info(home, prefix=home.data_dir, record=record.make_record(
        venv=home.data_dir, stable_venv=home.data_dir, built_by="installer", commit="a38a3fa" + "0" * 33,
        branch="main", built_at="2026-10-13T12:00:00.000Z"))
    assert record.format_version_line(info) == \
        f"HomeMesh {__version__} (commit a38a3fa, branch main, installed 2026-10-13)"
    info = info.model_copy(update={"dirty": True, "branch": None})
    assert record.format_version_line(info) == \
        f"HomeMesh {__version__} (commit a38a3fa, local changes, installed 2026-10-13)"


def test_service_python_follows_the_stable_path(tmp_path):
    prefix, _venv, rec = _install(tmp_path)
    assert record.service_python(rec) is None  # nothing at <prefix>/venv yet
    exe = record.stable_python(prefix / "venv")
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert record.service_python(rec) == exe
    if sys.platform == "win32":
        assert exe.name == "pythonw.exe" and record.stable_python(prefix / "venv", console=True).name == "python.exe"
    else:
        assert exe == prefix / "venv" / "bin" / "python"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_checkout_facts(tmp_path):
    git = shutil.which("git")
    src = tmp_path / "src"
    src.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x"}

    def g(*args):
        subprocess.run([git, "-C", str(src), *args], check=True, capture_output=True, env=env)

    assert record.checkout_facts(src, git)["commit"] is None  # not a checkout
    g("init", "-q", "-b", "main")
    assert record.checkout_facts(src, git)["commit"] is None  # no commit yet
    (src / "a.txt").write_text("1", encoding="utf-8")
    g("add", "a.txt")
    g("commit", "-q", "-m", "first")
    facts = record.checkout_facts(src, git)
    assert len(facts["commit"]) in (40, 64) and facts["branch"] == "main" and facts["dirty"] is False
    assert facts["remote"] is None and facts["commit_date"]
    (src / "a.txt").write_text("2", encoding="utf-8")
    (src / "untracked.txt").write_text("x", encoding="utf-8")
    g("config", "branch.main.remote", "origin")
    facts = record.checkout_facts(src, git)
    assert facts["dirty"] is True and facts["remote"] == "origin"
    assert record.checkout_facts(src, None) == dict.fromkeys(facts)
