"""The runner's check mode (design section 10) against real temp git repos (bare origin + clone): what it
reports, how it maps git's failures, and that hostile names, subjects and URLs can't get through."""

import json
import os
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from homemesh.protocol.update import UpdateCheckResult, installer_command
from homemesh.update import consent, files, launch, runner
from update_support import Layout, Repo, git


@pytest.fixture
def env(tmp_path):
    return Layout(tmp_path, Repo(tmp_path / "repo"))


@pytest.fixture
def hub(tmp_path):
    return Layout(tmp_path, Repo(tmp_path / "repo"), roles=("coordinator", "worker"))


def _check(layout, hooks=None, **kw):
    args = layout.args("check", **kw)
    code = runner.run_check(args, hooks or runner.Hooks())
    assert code == runner.EXIT_OK
    data = files.read_json(args["out"])
    assert data is not None
    UpdateCheckResult.model_validate(data)  # every result the runner writes is valid for the worker
    return data, args


def _outputs(layout, args, data):
    """Everything a check leaves behind that someone could read: result, log, leftovers in checks/."""
    texts = [json.dumps(data, ensure_ascii=False), Path(args["log"]).read_text(encoding="utf-8")]
    for p in files.checks_dir(layout.paths).iterdir():
        texts.append(p.read_text(encoding="utf-8", errors="replace"))
    return texts


# --------------------------------------------------------------------------- results

def test_up_to_date(env):
    data, args = _check(env)
    assert data["ok"] is True and data["behind"] == 0 and data["ahead"] == 0 and data["can_update"] is False
    assert data["upstream_commit"] == data["installed_commit"] == data["head_commit"] == env.old_commit
    assert data["branch"] == "main" and data["remote"] == "origin" and data["upstream_ref"] == "origin/main"
    assert data["commits"] == [] and data["allowed"] is True and data["problems"] == []
    assert data["web_changed"] is None and data["migrations"] is None  # worker: no web build, no database
    assert stat.S_IMODE(os.stat(args["out"]).st_mode) == 0o600 or sys.platform == "win32"
    # no leftovers besides the launcher's args file and the result
    assert sorted(p.name for p in files.checks_dir(env.paths).iterdir()) == [
        f"{args['run_id']}.args.json", f"{args['run_id']}.json"]


def test_behind_lists_the_commits_newest_first_with_unicode_subjects(env):
    first = env.repo.commit({"a.txt": "1"}, "Łódź: first")
    second = env.repo.commit({"b.txt": "2"}, "修复 → second\n\nbody text")
    data, _ = _check(env)
    assert data["ok"] and data["behind"] == 2 and data["can_update"] is True
    assert data["upstream_commit"] == second and data["upstream_date"]
    assert [c["sha"] for c in data["commits"]] == [second, first]
    assert [c["subject"] for c in data["commits"]] == ["修复 → second", "Łódź: first"]
    assert all(c["date"] for c in data["commits"]) and data["commits_truncated"] is False
    assert env.repo.head() == env.old_commit  # a check never moves the checkout


def test_many_commits_are_truncated(env):
    env.repo.empty_commits(51)
    env.repo.commit({"n.txt": "last"}, "the last one")
    data, _ = _check(env)
    assert data["behind"] == 52 and len(data["commits"]) == 50 and data["commits_truncated"] is True
    assert data["commits"][0]["subject"] == "the last one"


def test_control_characters_in_a_subject_are_stripped(env):
    env.repo.commit({"x": "1"}, b"Fix \x1b[2K\rspoofed\x07 \x9bline")
    data, args = _check(env)
    subject = data["commits"][0]["subject"]
    assert subject == "Fix [2Kspoofed line"
    for ch in ("\x1b", "\r", "\x07", "\x9b"):
        assert all(ch not in text for text in _outputs(env, args, data))


def test_hub_sees_web_changes_and_migrations(hub):
    hub.repo.commit({"apps/web/src/new.ts": "export {}\n",
                     "src/homemesh/coordinator/db/migrations/0004_four.sql": "-- 4\n"}, "web + migration")
    data, _ = _check(hub, node_dir=None)
    assert data["web_changed"] is True and data["migrations"] is True
    assert isinstance(data["node_found"], bool)


def test_hub_without_web_changes(hub):
    hub.repo.commit({"README": "x"}, "docs only")
    data, _ = _check(hub)
    assert data["web_changed"] is False and data["migrations"] is False and data["can_update"] is True


def test_web_change_without_node_is_blocking(hub, tmp_path, monkeypatch):
    hub.repo.commit({"apps/web/src/new.ts": "export {}\n"}, "web")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    data, _ = _check(hub)
    assert data["node_found"] is False and data["can_update"] is False
    assert [p["code"] for p in data["problems"] if p["blocking"]] == ["update_node_missing"]


def test_node_dir_layouts_are_found(env, tmp_path):
    run = runner._Check(env.args("check"), runner.Hooks())
    assert run.find_node() is None or isinstance(run.find_node(), tuple)  # whatever this machine has
    posix = tmp_path / "node-posix"
    (posix / "bin").mkdir(parents=True)
    exe = "node.exe" if sys.platform == "win32" else "node"
    (posix / "bin" / exe).write_text("")
    cli = posix / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("")
    run = runner._Check(env.args("check", node_dir=str(posix / "bin")), runner.Hooks())
    assert run.find_node() == (str(posix / "bin" / exe), os.path.normpath(cli))
    win = tmp_path / "node-win"
    wcli = win / "node_modules" / "npm" / "bin" / "npm-cli.js"
    wcli.parent.mkdir(parents=True)
    wcli.write_text("")
    (win / exe).write_text("")
    run = runner._Check(env.args("check", node_dir=str(win)), runner.Hooks())
    assert run.find_node() == (str(win / exe), str(wcli))


# --------------------------------------------------------------------------- refusals

def test_dirty_checkout(env):
    (env.repo.src / "behavior.json").write_text('{"local": "edit"}')
    data, _ = _check(env)
    assert data["ok"] is False and data["error"]["code"] == "update_dirty_checkout"
    assert "behavior.json" in data["error"]["message"] and str(env.repo.src) in data["error"]["hint"]
    assert data["allowed"] is True


def test_untracked_files_are_not_dirty(env):
    (env.repo.src / "notes.txt").write_text("mine")
    data, _ = _check(env)
    assert data["ok"] is True


def test_diverged(env):
    (env.repo.src / "local.txt").write_text("x")
    git("-C", str(env.repo.src), "add", "local.txt")
    git("-C", str(env.repo.src), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "local")
    env.repo.commit({"u.txt": "1"}, "upstream")
    data, _ = _check(env)
    assert data["ok"] and data["ahead"] == 1 and data["behind"] == 1 and data["can_update"] is False
    assert [(p["code"], p["blocking"]) for p in data["problems"]] == [("update_diverged", True)]
    assert "origin/main" in data["problems"][0]["message"]


def test_detached_head(env):
    git("-C", str(env.repo.src), "checkout", "-q", "--detach")
    data, _ = _check(env)
    assert data["error"]["code"] == "update_detached_head"


def test_no_upstream(env):
    git("-C", str(env.repo.src), "branch", "--unset-upstream")
    data, _ = _check(env)
    assert data["error"]["code"] == "update_no_upstream" and "main" in data["error"]["message"]


def test_not_a_checkout(env):
    import shutil

    shutil.rmtree(env.repo.src / ".git")
    data, _ = _check(env)
    assert data["error"]["code"] == "update_not_git_checkout"


def test_unreachable_remote(env, tmp_path):
    git("-C", str(env.repo.src), "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    data, _ = _check(env)
    assert data["ok"] is False and data["error"]["code"] == "update_fetch_failed"
    assert data["error"]["message"].startswith("Could not fetch from origin:")
    assert "does not appear to be a git repository" in data["error"]["message"]  # git's reason, not its advice


@pytest.mark.parametrize("stderr, detail", [
    ("fatal: unable to access 'https://github.com/me/private.git/': Could not resolve host: github.com\n",
     "unable to access '<remote address>': Could not resolve host: github.com"),
    ("fatal: 'git@github.com:me/private.git' does not appear to be a git repository\n"
     "fatal: Could not read from remote repository.\n",
     "'<remote address>' does not appear to be a git repository"),
    ("ssh: connect to host example.invalid port 22: Network is unreachable\n"
     "fatal: Could not read from remote repository.\n\nPlease make sure you have the correct access rights\n",
     "Could not read from remote repository"),
    ("something odd\n", "something odd")])
def test_fetch_failures_are_explained_without_the_remote_address(stderr, detail):
    assert runner._fetch_detail(stderr).rstrip(".") == detail


def test_remote_name_that_looks_like_an_option_is_refused(env, tmp_path):
    marker = tmp_path / "pwned"
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    git("-C", str(env.repo.src), "config", "branch.main.remote", f"--upload-pack={script}")
    data, _ = _check(env)
    assert data["error"]["code"] == "update_bad_ref_name" and "--upload-pack" in data["error"]["message"]
    assert not marker.exists()


@pytest.mark.parametrize("name", ["a..b", "-x", "x.lock", "x/", ".x", "a//b", "a b"])
def test_branch_names_git_would_misread_are_refused(name):
    assert runner.is_ref_name(name) is False
    assert runner.is_ref_name("feature/x-1.2_y") is True


def test_higher_layout_needs_the_installer(env):
    env.repo.commit({"src/homemesh/update/layout.txt": "2\n"}, "layout 2")
    data, _ = _check(env)
    blocking = [p for p in data["problems"] if p["blocking"]]
    assert [p["code"] for p in blocking] == ["update_needs_installer"] and data["can_update"] is False
    assert blocking[0]["hint"] == installer_command("windows" if sys.platform == "win32" else "linux",
                                                    str(env.repo.src), pull=True)


def test_low_disk_is_blocking(env):
    env.repo.commit({"x": "1"}, "x")
    hooks = runner.Hooks()
    hooks.disk_free = lambda path: 100 * 1024 * 1024
    data, _ = _check(env, hooks)
    low = [p for p in data["problems"] if p["code"] == "update_low_disk"]
    assert low and low[0]["blocking"] and "100.0 MiB free" in low[0]["message"] and not data["can_update"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="systemd linger is Linux-only")
def test_no_linger_is_a_warning(env, tmp_path):
    env.repo.commit({"x": "1"}, "x")
    hooks = runner.Hooks()
    hooks.linger_dir = str(tmp_path / "linger")
    os.mkdir(hooks.linger_dir)
    data, _ = _check(env, hooks, service="systemd")
    assert [(p["code"], p["blocking"]) for p in data["problems"]] == [("no_linger", False)]
    assert data["can_update"] is True


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="systemd linger is Linux-only")
def test_no_linger_folder_at_all_is_a_warning_too(env, tmp_path):
    hooks = runner.Hooks()
    hooks.linger_dir = str(tmp_path / "never-created")  # logind creates it only when lingering is enabled
    data, _ = _check(env, hooks, service="systemd")
    assert [p["code"] for p in data["problems"]] == ["no_linger"]
    hooks.linger_dir = str(tmp_path / "linger")
    os.makedirs(hooks.linger_dir)
    import pwd

    user = os.environ.get("USER") or os.environ.get("LOGNAME") or pwd.getpwuid(os.getuid()).pw_name
    Path(hooks.linger_dir, user).write_text("")
    data, _ = _check(env, hooks, service="systemd")
    assert data["problems"] == []


def test_installed_commit_unknown_counts_from_head(env):
    rec_file = Path(os.path.realpath(env.stable)) / "homemesh-install.json"
    rec = json.loads(rec_file.read_text())
    rec["commit"] = "f" * 40  # not in this repository (e.g. installed from another clone)
    rec_file.write_text(json.dumps(rec))
    env.repo.commit({"x": "1"}, "x")
    data, _ = _check(env)
    assert data["installed_commit"] == "f" * 40 and data["behind"] == 1
    assert [(p["code"], p["blocking"]) for p in data["problems"]] == [("installed_commit_unknown", False)]


def test_not_allowed_without_consent_for_remote_checks_only(env):
    consent.disallow(env.paths)
    data, args = _check(env)
    assert data["ok"] is False and data["error"]["code"] == "update_not_allowed" and data["allowed"] is False
    assert "fetch" not in Path(args["log"]).read_text()
    data, _ = _check(env, origin="local")
    assert data["ok"] is True and data["allowed"] is False


def test_busy_writes_nothing(env):
    lock = files.update_lock(env.paths)
    assert lock.acquire()
    try:
        args = env.args("check")
        assert runner.run_check(args) == runner.EXIT_BUSY
        assert files.read_json(args["out"]) is None
        assert f"{runner.EXIT_MARKER} {runner.EXIT_BUSY}" in Path(args["log"]).read_text()
    finally:
        lock.release()


def test_a_crashing_check_logs_redacted_and_exits_marked(env):
    def boom(path):
        raise RuntimeError("disk probe exploded at https://me:ghp_SECRETtoken99@example.invalid/x")

    hooks = runner.Hooks()
    hooks.disk_free = boom
    args = env.args("check")
    assert runner.run_check(args, hooks) == runner.EXIT_FAILED
    assert files.read_json(args["out"]) is None and not files.update_lock_held(env.paths)
    log = Path(args["log"]).read_text(encoding="utf-8")
    assert "unexpected error" in log and "RuntimeError" in log and f"{runner.EXIT_MARKER} {runner.EXIT_FAILED}" in log
    assert "ghp_SECRETtoken99" not in log and "me:" not in log


def test_rev_parse_end_of_options_only_where_git_supports_it(env):
    env.repo.commit({"x": "1"}, "x")
    run = runner._Check(env.args("check"), runner.Hooks())
    run.check_git()
    assert run.git_version >= (2, 24)
    up = run.source_state()
    run.fetch(up)
    expected = git("-C", str(env.repo.src), "rev-parse", "origin/main")
    for version, eoo in (((2, 29), ()), ((2, 30), ("--end-of-options",))):
        run.git_version = version
        assert run.rev_parse_eoo() == eoo
        assert run.resolve_upstream(up) == expected  # real git resolves it either way


def test_git_missing(env, tmp_path):
    data, _ = _check(env, git_path=str(tmp_path / "no-git"))
    assert data["error"]["code"] == "update_git_missing"


# --------------------------------------------------------------------------- credentials

def test_credentials_in_the_remote_url_are_a_warning_and_never_reported(env):
    url = "https://someone:ghp_SECRETtoken123456@example.invalid/team/homemesh.git"
    git("-C", str(env.repo.src), "remote", "set-url", "origin", url)
    git("-C", str(env.repo.src), "config", f"url.{env.repo.origin}.insteadOf", url)  # fetch still works
    env.repo.commit({"x": "1"}, "x")
    data, args = _check(env)
    assert data["ok"] and data["behind"] == 1
    assert [(p["code"], p["blocking"]) for p in data["problems"]] == [("remote_url_has_credentials", False)]
    for text in _outputs(env, args, data):
        assert "ghp_SECRETtoken123456" not in text and "someone:" not in text and "example.invalid" not in text


@pytest.mark.parametrize("url, has", [
    ("https://user:pw@host/x.git", True), ("https://ghp_abc@host/x.git", True), ("https://x@host/r", True),
    ("ssh://git@host/x.git", False), ("git@github.com:o/r.git", False), ("https://host/x.git", False),
    ("ssh://user:pw@host/x", True)])
def test_url_credential_detection(url, has):
    assert runner._url_has_credentials(url) is has


@pytest.mark.skipif(sys.platform == "win32", reason="Git Credential Manager's wording on Windows is not verified")
def test_http_401_is_auth_failed_and_no_askpass_runs(env, tmp_path, monkeypatch):
    class Deny(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="homemesh-test"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Deny)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        marker = tmp_path / "askpass-called"
        askpass = tmp_path / "askpass.sh"
        askpass.write_text(f"#!/bin/sh\necho called >> {marker}\necho hunter2\n")
        askpass.chmod(0o755)
        for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("SSH_ASKPASS", str(askpass))
        monkeypatch.setenv("GIT_ASKPASS", str(askpass))
        monkeypatch.setenv("SSH_ASKPASS_REQUIRE", "force")
        monkeypatch.setenv("DISPLAY", ":0")
        url = f"http://127.0.0.1:{server.server_address[1]}/homemesh.git"
        git("-C", str(env.repo.src), "remote", "set-url", "origin", url)
        # core.askPass (config, not environment) is used when GIT_ASKPASS is unset, but not when it is set
        # and empty: this is what makes the runner's GIT_ASKPASS="" matter.
        git("-C", str(env.repo.src), "config", "core.askPass", str(askpass))
        data, _ = _check(env)
        assert data["error"]["code"] == "update_auth_failed", data
        assert not marker.exists(), "an askpass helper was run"
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- the real invocation

def _base_python():
    return getattr(sys, "_base_executable", None) or sys.executable


def test_runner_script_runs_isolated_on_the_base_interpreter(env):
    env.repo.commit({"x": "1"}, "x")
    args = env.args("check")
    args_file = files.check_args_file(env.paths, args["run_id"])
    proc = subprocess.run([_base_python(), "-I", "-S", "-X", "utf8", runner.__file__, "check", "--args",
                           str(args_file)], capture_output=True, timeout=120, check=False,
                          env={k: v for k, v in os.environ.items() if not k.startswith("PYTHON")})
    assert proc.returncode == runner.EXIT_OK, proc.stderr.decode()
    data = files.read_check_result(env.paths, args["run_id"])
    assert UpdateCheckResult.model_validate(data).behind == 1


@pytest.mark.skipif(sys.prefix == sys.base_prefix, reason="needs to run from a venv")
def test_runner_refuses_a_venv_interpreter(env):
    args = env.args("check")
    proc = subprocess.run([sys.executable, "-I", "-S", runner.__file__, "check", "--args",
                           str(files.check_args_file(env.paths, args["run_id"]))], capture_output=True, timeout=60,
                          check=False)
    assert proc.returncode == runner.EXIT_NO_BASE_PYTHON
    assert files.read_json(args["out"]) is None


def test_an_early_exit_writes_its_marker_into_the_log_once_and_intact(env):
    """stderr is the log too. On Windows that inherited handle keeps its own file position, so a second copy
    of the marker written through it would land on top of the lines logged before it."""
    args = env.args("check")
    lock = files.update_lock(env.paths)
    assert lock.acquire()
    try:
        with files.open_private_append(Path(args["log"])) as log:
            proc = subprocess.run([_base_python(), "-I", "-S", "-X", "utf8", runner.__file__, "check", "--args",
                                   str(files.check_args_file(env.paths, args["run_id"]))], stdout=log, stderr=log,
                                  timeout=60, check=False,
                                  env={k: v for k, v in os.environ.items() if not k.startswith("PYTHON")})
    finally:
        lock.release()
    assert proc.returncode == runner.EXIT_BUSY
    text = Path(args["log"]).read_text(encoding="utf-8")
    assert text.count(runner.EXIT_MARKER) == 1, text
    assert f"{runner.EXIT_MARKER} {runner.EXIT_BUSY}: another update or check is running (update.lock is held)\n" in text
    assert launch._exit_code_from_log(Path(args["log"])) == runner.EXIT_BUSY


def test_invalid_args_file_exits_2_and_writes_nothing(env, tmp_path):
    args = env.args("check")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**args, "git": "git"}))  # not absolute
    proc = subprocess.run([_base_python(), "-I", "-S", runner.__file__, "check", "--args", str(bad)],
                          capture_output=True, timeout=60, check=False,
                          env={k: v for k, v in os.environ.items() if not k.startswith("PYTHON")})
    assert proc.returncode == runner.EXIT_BAD_ARGS
    assert b"git: must be an absolute path" in proc.stderr and runner.EXIT_MARKER.encode() in proc.stderr
