"""Every third-party import in the package must be a declared dependency.

CI found `cryptography` imported by `qel.evaluator.core` but absent from
`pyproject.toml` and `make setup`, so a fresh checkout could not even collect
four test modules. Fixing the install line alone would let it happen again;
this test fails as soon as a new third-party import is used without declaring
the package.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "python" / "qel"

# Test-only tooling, not a runtime dependency of the package.
DEV_ONLY = {"pytest"}


def _declared() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    names = set()
    for requirement in data["project"]["dependencies"]:
        # "jsonschema==4.25.1" -> "jsonschema"
        name = requirement.split(";")[0].strip()
        for separator in ("==", ">=", "<=", "~=", "!=", ">", "<", "["):
            name = name.split(separator)[0]
        names.add(name.strip().lower().replace("-", "_"))
    return names


def _imported() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level or node.module is None:
                    continue
                modules = [node.module]
            else:
                continue
            for module in modules:
                top = module.split(".")[0]
                if top == "qel" or top in sys.stdlib_module_names:
                    continue
                found.setdefault(top, set()).add(str(path.relative_to(ROOT)))
    return found


def test_every_third_party_import_is_declared() -> None:
    declared = _declared()
    missing = {
        module: sorted(files)
        for module, files in _imported().items()
        if module not in declared and module not in DEV_ONLY
    }
    assert missing == {}, (
        "undeclared third-party imports (add them to pyproject.toml dependencies, "
        f"make setup, and the CI install step): {missing}"
    )


def test_cryptography_is_declared() -> None:
    # The exact regression CI caught; kept explicit so the guard is never
    # weakened into a no-op by an allowlist edit.
    assert "cryptography" in _declared()


@pytest.mark.parametrize("module", ["cryptography", "jsonschema", "numpy"])
def test_declared_modules_import(module: str) -> None:
    __import__(module)
