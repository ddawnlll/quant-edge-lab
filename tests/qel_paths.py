"""Portable resolution of locally built QEL binaries.

Tests that need a compiled binary must not hardcode an operator's home
directory. Resolution order, first hit wins:

1. an explicit environment override (``QEL_NATIVE_RUNNER`` etc.),
2. the repo-local build caches used by ``make`` (``.build-cache/*/release``),
3. a ``release`` directory under the crate itself,
4. ``PATH``.

A missing binary is a skip, never a silent pass: callers should use
``require`` so an absent artifact is reported rather than assumed.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# name -> (environment override, cache subdirectory, crate directory)
_BINARIES: dict[str, tuple[str, str, str]] = {
    "qel-native-runner": ("QEL_NATIVE_RUNNER", "qel-native", "native/runner"),
    "qel-passive-runner": ("QEL_PASSIVE_RUNNER", "qel-native", "native/passive_runner"),
    "qel-local-view": ("QEL_LOCAL_VIEW_RUNNER", "qel-native", "native/local_view"),
    "qel-parquet-runner": ("QEL_PARQUET_RUNNER", "qel-native", "native/runner"),
    "qel-ingest": ("QEL_INGEST", "qel-data-engine", "data-engine"),
    "qel-native-spike": ("QEL_NATIVE_SPIKE", "qel-native", "native/spike"),
}

BUILD_CACHE = Path(os.environ.get("QEL_BUILD_CACHE", ROOT / ".build-cache"))


def candidates(name: str) -> list[Path]:
    override, cache_dir, crate = _BINARIES.get(name, (f"QEL_{name.upper().replace('-', '_')}", "qel-native", ""))
    found: list[Path] = []
    env_value = os.environ.get(override)
    if env_value:
        found.append(Path(env_value))
    found.append(BUILD_CACHE / cache_dir / "release" / name)
    if crate:
        found.append(ROOT / crate / "target" / "release" / name)
    on_path = shutil.which(name)
    if on_path:
        found.append(Path(on_path))
    return found


def find(name: str) -> Path | None:
    """Return the first existing binary path for ``name``, else ``None``."""
    for path in candidates(name):
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def describe(name: str) -> str:
    """Human-readable resolution attempt, used in skip reasons."""
    return f"{name} not built; looked in: " + ", ".join(str(p) for p in candidates(name))
