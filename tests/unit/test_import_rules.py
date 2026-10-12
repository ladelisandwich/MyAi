"""Architecture guard: lower layers must not import higher ones (no circular dependencies)."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "homemesh"
FORBIDDEN = {
    "common": {"protocol", "hardware", "inference", "scheduler", "memory", "knowledge", "tools", "coordinator", "worker", "cli", "runtime"},
    "protocol": {"hardware", "inference", "scheduler", "memory", "knowledge", "tools", "coordinator", "worker", "cli", "runtime"},
    "scheduler": {"inference", "memory", "knowledge", "tools", "coordinator", "worker", "cli", "runtime", "hardware"},
    "hardware": {"inference", "scheduler", "memory", "knowledge", "tools", "coordinator", "worker", "cli", "runtime"},
    "inference": {"scheduler", "memory", "knowledge", "tools", "coordinator", "worker", "cli", "runtime"},
    "memory": {"coordinator", "worker", "cli", "runtime", "inference"},
    "knowledge": {"coordinator", "worker", "cli", "runtime", "inference"},
    "tools": {"coordinator", "worker", "cli", "runtime"},
    "worker": {"coordinator", "cli", "runtime"},
    "coordinator": {"worker", "cli", "runtime"},
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("homemesh."):
            out.add(node.module.split(".")[1])
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


def test_os_specific_code_isolated():
    # winreg / Windows-only calls must live in hardware/ or cli/service.py only
    for f in SRC.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        if "import winreg" in text or "schtasks" in text:
            assert f.parent.name == "hardware" or f.name == "service.py", f
