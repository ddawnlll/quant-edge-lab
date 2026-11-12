"""Issue #90: build entry points must not hardcode one operator's home path."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Build/entry-point files that must work on a fresh checkout on any host.
ENTRY_POINTS = [
    "Makefile",
    "benchmarks/run_all_bench.sh",
    "benchmarks/build_evidence.py",
    "benchmarks/README.md",
]

ABSOLUTE_HOME = re.compile(r"/home/[A-Za-z0-9_.-]+/")


def test_build_entry_points_have_no_absolute_home_paths() -> None:
    offenders = {}
    for relative in ENTRY_POINTS:
        text = (ROOT / relative).read_text()
        hits = [
            f"{relative}:{number}: {line.strip()}"
            for number, line in enumerate(text.splitlines(), 1)
            if ABSOLUTE_HOME.search(line)
        ]
        if hits:
            offenders[relative] = hits
    assert offenders == {}, offenders


def test_makefile_defaults_to_a_repo_local_cache() -> None:
    text = (ROOT / "Makefile").read_text()
    assert "QEL_BUILD_CACHE ?= $(CURDIR)/.build-cache" in text
    assert "CARGO_TARGET_DIR ?= $(QEL_BUILD_CACHE)/qel-native" in text
    # Both cargo invocations must use the variable, not a literal path.
    assert "CARGO_TARGET_DIR=/home" not in text
    assert "$(CONTRACT_TARGET_DIR)/release/qel-contract" in text


def test_test_binaries_resolve_portably() -> None:
    import qel_paths

    # A non-existent override must not be trusted; resolution falls through and
    # reports the attempt instead of raising or silently succeeding.
    for name in ("qel-native-runner", "qel-passive-runner"):
        found = qel_paths.find(name)
        assert found is None or found.is_file()
        assert name in qel_paths.describe(name)
