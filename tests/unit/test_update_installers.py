"""Installers and uninstallers for the blue/green layout (design 3.1 and 16).

The Linux scripts run for real against a throw-away HOME with a stub Python (no pip, no network): the
first install creates ``venv -> venvs/<id>``, an old real ``venv`` folder is converted to
``venvs/legacy-<time>``, upgrades flip the link and keep only the current and the previous version, and
the uninstaller removes exactly the generated names. The PowerShell scripts can't run here; they are
checked for the rules that matter on Windows PowerShell 5.1. The real installers run in CI
(.github/workflows/install.yml, scripts/ci/*.sh).
"""

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from homemesh.protocol.update import VENV_ID_PATTERN

ROOT = Path(__file__).resolve().parents[2]
LINUX = ROOT / "deploy" / "linux"
WINDOWS = ROOT / "deploy" / "windows"
BASH = shutil.which("bash")
posix_bash = pytest.mark.skipif(sys.platform == "win32" or BASH is None, reason="needs bash on Linux")

# A stand-in for "python3": the version/feature checks go to the real interpreter; "-m venv DIR" makes a
# venv whose python and homemesh only log their arguments (pip, record-install, version, service).
FAKE_PYTHON = r"""#!/usr/bin/env bash
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
  mkdir -p "$3/bin"
  cat > "$3/bin/python" <<'EOF'
#!/usr/bin/env bash
venv="$(cd "$(dirname "$0")/.." && pwd -P)"
echo "$venv $*" >> "$FAKE_LOG"
if [ "$*" = "-m homemesh version" ]; then echo "HomeMesh stub"; fi
case "$*" in *record-install*) echo '{"v": 1}' > "$venv/homemesh-install.json" ;; esac
exit 0
EOF
  cat > "$3/bin/homemesh" <<'EOF'
#!/usr/bin/env bash
echo "homemesh $*" >> "$FAKE_LOG"
[ "${1:-}" = status ] && exit 3
[ "${1:-}" = version ] && echo "HomeMesh stub"
exit 0
EOF
  chmod +x "$3/bin/python" "$3/bin/homemesh"
  exit 0
fi
exec "$REAL_PYTHON" "$@"
"""


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake = tmp_path / "python3-stub"
    fake.write_text(FAKE_PYTHON, encoding="utf-8")
    fake.chmod(0o755)
    log = tmp_path / "calls.log"
    env = {"HOME": str(home), "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin", "FAKE_LOG": str(log),
           "REAL_PYTHON": sys.executable, "LANG": "C.UTF-8"}
    return home, fake, log, env


def _run(script, *args, env):
    return subprocess.run([BASH, str(script), *args], env=env, capture_output=True, text=True, timeout=120)


def _venvs(home):
    return sorted(p.name for p in (home / ".local/share/homemesh/venvs").iterdir())


def _install(fake, env, *extra):
    time.sleep(1.05)  # venv ids carry a timestamp in seconds
    r = _run(LINUX / "install.sh", "--no-web", "--python", str(fake), *extra, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return r


@posix_bash
@pytest.mark.slow
def test_linux_installer_builds_aside_switches_the_link_and_prunes(sandbox):
    home, fake, log, env = sandbox
    share = home / ".local/share/homemesh"
    venv = share / "venv"
    # An install from before 0.3.0: a real venv folder (here with a data file next to it, as on Linux the
    # prefix is the data folder).
    (venv / "bin").mkdir(parents=True)
    old_cli = venv / "bin" / "homemesh"
    old_cli.write_text("#!/usr/bin/env bash\necho \"old $*\" >> \"$FAKE_LOG\"\n[ \"$1\" = status ] && exit 3\n"
                       "exit 0\n", encoding="utf-8")
    old_cli.chmod(0o755)
    (share / "homemesh.sqlite3").write_text("data", encoding="utf-8")

    r = _install(fake, env, "--upgrade")
    assert venv.is_symlink() and os.readlink(venv).startswith("venvs/")
    names = _venvs(home)
    assert len(names) == 2 and all(re.fullmatch(VENV_ID_PATTERN.strip("^$"), n) for n in names)
    legacy = [n for n in names if n.startswith("legacy-")]
    assert legacy and (share / "venvs" / legacy[0] / "bin" / "homemesh").exists(), "the old folder is kept"
    current = os.readlink(venv).split("/", 1)[1]
    assert re.fullmatch(r"([0-9a-f]{12}|nogit)-[0-9]{14}", current), "named after the commit"
    calls = log.read_text(encoding="utf-8")
    assert "old status" in calls, "the installed (old) version is asked whether HomeMesh runs"
    assert f"update record-install --source {ROOT} --stable-venv {venv} --built-by installer" in calls
    assert (venv / "homemesh-install.json").exists() and (home / ".local/bin/homemesh").is_symlink()
    assert "Moving the previous program folder" in r.stdout

    _install(fake, env, "--upgrade")  # the new one and the previous one are kept, the legacy folder goes
    second = os.readlink(venv).split("/", 1)[1]
    assert second != current and _venvs(home) == sorted([current, second])
    _install(fake, env, "--upgrade")
    third = os.readlink(venv).split("/", 1)[1]
    assert _venvs(home) == sorted([second, third])
    assert (share / "homemesh.sqlite3").read_text(encoding="utf-8") == "data"

    r = _run(LINUX / "install.sh", "--no-web", "--python", str(fake), env=env)
    assert r.returncode == 0 and "already installed" in r.stdout and len(_venvs(home)) == 2

    # Uninstall: the link and every generated folder go, anything else (data, unknown folders) stays.
    (share / "venvs" / "keep-me").mkdir()
    (share / "b" / "0123abcd").mkdir(parents=True)
    (share / "b" / "notes").mkdir()
    r = _run(LINUX / "uninstall.sh", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not venv.exists() and not venv.is_symlink() and not (home / ".local/bin/homemesh").exists()
    assert _venvs(home) == ["keep-me"] and sorted(p.name for p in (share / "b").iterdir()) == ["notes"]
    assert (share / "homemesh.sqlite3").read_text(encoding="utf-8") == "data"


@posix_bash
def test_a_failed_build_changes_nothing(sandbox):
    home, fake, _log, env = sandbox
    _install(fake, env)
    before = os.readlink(home / ".local/share/homemesh/venv")
    broken = fake.parent / "python3-broken"
    broken.write_text(FAKE_PYTHON.replace('echo "$venv $*" >> "$FAKE_LOG"',
                                          'echo "$venv $*" >> "$FAKE_LOG"; [ "$2" = pip ] && exit 1'),
                      encoding="utf-8")
    broken.chmod(0o755)
    time.sleep(1.05)
    r = _run(LINUX / "install.sh", "--no-web", "--upgrade", "--python", str(broken), env=env)
    assert r.returncode != 0
    assert os.readlink(home / ".local/share/homemesh/venv") == before and _venvs(home) == [before.split("/")[1]]


@posix_bash
def test_shell_scripts_parse():
    for script in (LINUX / "install.sh", LINUX / "uninstall.sh"):
        r = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_installers_use_the_same_venv_id_rule_as_the_updater():
    pattern = VENV_ID_PATTERN
    assert f"ID_RE='{pattern}'" in (LINUX / "install.sh").read_text(encoding="utf-8")
    assert f"ID_RE='{pattern}'" in (LINUX / "uninstall.sh").read_text(encoding="utf-8")
    ps = (WINDOWS / "install.ps1").read_text(encoding="utf-8")
    assert f"$IdPattern = '{pattern}'" in ps and "-cmatch $IdPattern" in ps  # case-sensitive (-match is not)


def test_windows_scripts_switch_junctions_without_following_them():
    install = (WINDOWS / "install.ps1").read_text(encoding="utf-8")
    uninstall = (WINDOWS / "uninstall.ps1").read_text(encoding="utf-8")
    assert "New-Item -ItemType Junction -Path $next -Target $NewVenv" in install
    assert "[IO.Directory]::Delete($Path, $false)" in install and "[IO.Directory]::Delete($path, $false)" in uninstall
    assert "update\", \"record-install\"" in install and "Split-Path -Parent $node.Source" in install
    assert "Get-ProcessesUnder @($Venv, $Venvs)" in install
    # The junction goes before the recursive delete of the program folder.
    assert uninstall.index("[IO.Directory]::Delete") < uninstall.index("Remove-Item -Recurse -Force $Prefix")
    # Launchers keep using the stable path.
    assert '%~dp0..\\venv\\Scripts\\python.exe' in install


@pytest.mark.parametrize("name", ["install.ps1", "uninstall.ps1"])
def test_powershell_code_is_ascii(name):
    """Windows PowerShell 5.1 reads a script without BOM in the ANSI code page: non-ASCII text in code
    (strings) would be garbled. Comments may contain it."""
    for n, line in enumerate((WINDOWS / name).read_text(encoding="utf-8").splitlines(), 1):
        if not line.isascii():
            assert line.lstrip().startswith("#"), f"{name}:{n}: non-ASCII outside a comment: {line!r}"
