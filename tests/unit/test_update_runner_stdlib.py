"""The update runner runs as ``python -I -S runner.py`` from the base interpreter: standard library only,
one file, no ``homemesh`` imports. And the launcher's pure helpers that point at it."""

import ast
import sys
from pathlib import Path

import pytest

from homemesh.update import launch, runner

RUNNER = Path(__file__).resolve().parents[2] / "src" / "homemesh" / "update" / "runner.py"


def _imports(tree: ast.AST) -> list[tuple[str, int]]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(a.name, 0) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append((node.module or "", node.level))
    return out


def test_runner_imports_only_the_standard_library():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    imports = _imports(tree)
    assert imports, "parsed nothing?"
    bad = [(name, level) for name, level in imports
           if level > 0 or name.split(".")[0] not in sys.stdlib_module_names or name.split(".")[0] == "homemesh"]
    assert not bad, f"runner.py must import only the standard library (it runs with -I -S): {bad}"
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "__import__" not in calls


def test_runner_command_line():
    with pytest.raises(SystemExit) as exc:
        runner.main(["bogus", "--args", "/x.json"])
    assert exc.value.code == runner.EXIT_BAD_ARGS == 2
    with pytest.raises(SystemExit):
        runner.main(["check"])  # --args is required
    assert runner.MODES == ("check", "apply")
    assert 'if __name__ == "__main__":' in RUNNER.read_text(encoding="utf-8")


def test_launcher_points_at_this_runner(monkeypatch, tmp_path):
    assert launch.runner_script() == RUNNER.resolve()
    stub = tmp_path / "stub.py"
    stub.write_text("print('stub')\n", encoding="utf-8")
    monkeypatch.setenv(launch.RUNNER_ENV, str(stub))
    assert launch.runner_script() == RUNNER.resolve()  # ignored outside tests
    monkeypatch.setenv(launch.TESTING_ENV, "1")
    assert launch.runner_script() == stub.resolve()
    cmd = launch.runner_command(Path("/py/python3"), RUNNER, "apply", Path("/d/args.json"))
    assert cmd[1:5] == ["-I", "-S", "-X", "utf8"] and cmd[-3:] == ["apply", "--args", str(Path("/d/args.json"))]
    assert launch.unit_name("check", "u20261013120000-0123abcd") == "homemesh-update-check-u20261013120000-0123abcd"
