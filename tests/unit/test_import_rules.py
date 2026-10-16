"""Architecture guard: lower layers must not import higher ones (no circular dependencies)."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "homemesh"
PURE = {"protocol", "hardware", "inference", "scheduler", "memory", "knowledge", "tools", "update", "coordinator",
        "worker", "cli", "runtime"}
FORBIDDEN = {
    "common": PURE,
    "protocol": PURE - {"protocol"},
    "scheduler": {"inference", "memory", "knowledge", "tools", "coordinator", "worker", "cli", "runtime", "hardware",
                  "update"},
    "hardware": {"inference", "scheduler", "memory", "knowledge", "tools", "coordinator", "worker", "cli", "runtime",
                 "update"},
    "inference": {"scheduler", "memory", "knowledge", "tools", "coordinator", "worker", "cli", "runtime", "update"},
    "memory": {"coordinator", "worker", "cli", "runtime", "inference", "update"},
    "knowledge": {"coordinator", "worker", "cli", "runtime", "inference", "update"},
    "tools": {"coordinator", "worker", "cli", "runtime", "update"},
    "worker": {"coordinator", "cli", "runtime"},
    "coordinator": {"worker", "cli", "runtime"},
    # The updater's shared layer: used by worker, coordinator and cli, never the other way round, and
    # nothing below it imports it (update imports protocol and common).
    "update": {"coordinator", "worker", "cli", "runtime", "scheduler", "inference", "memory", "knowledge", "tools",
               "hardware"},
}


def _imports(path: Path, src: Path = SRC) -> set[str]:
    """Top-level ``homemesh`` subpackages/modules ``path`` imports, in every spelling: ``import homemesh.x``,
    ``from homemesh.x import y``, ``from homemesh import x`` and relative imports (``from ..x import y``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = ["homemesh", *path.relative_to(src).parts[:-1]]  # the package this file is in
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[:len(package) - (node.level - 1)]
                target = [*base, *(node.module.split(".") if node.module else [])]
            else:
                target = (node.module or "").split(".")
            if target[0] != "homemesh":
                continue
            if len(target) > 1:
                out.add(target[1])
            else:  # from homemesh import x, y
                out.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("homemesh."):
                    out.add(a.name.split(".")[1])
    return out


def test_layering():
    violations = []
    for pkg, forbidden in FORBIDDEN.items():
        for f in (SRC / pkg).rglob("*.py"):
            bad = _imports(f) & forbidden
            if bad:
                violations.append(f"{f.relative_to(SRC)} imports {sorted(bad)}")
    assert not violations, "\n".join(violations)


def test_the_rule_sees_every_import_spelling(tmp_path):
    pkg = tmp_path / "update"
    pkg.mkdir()
    probe = pkg / "probe.py"
    probe.write_text("from homemesh import coordinator\nfrom ..worker import agent\nfrom . import files\n"
                     "import homemesh.cli.main\nfrom homemesh.common.paths import AppPaths\n", encoding="utf-8")
    assert _imports(probe, tmp_path) == {"coordinator", "worker", "update", "cli", "common"}


def test_os_specific_code_isolated():
    # winreg / Windows-only calls must live in hardware/ or cli/service.py only
    for f in SRC.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        if "import winreg" in text or "schtasks" in text:
            assert f.parent.name == "hardware" or f.name == "service.py", f
