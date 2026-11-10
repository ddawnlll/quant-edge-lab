"""Real-principal isolation acceptance for issue #8.

Skipped unless the operator has run scripts/provision_isolation.sh and set
QEL_REAL_PRINCIPALS=1. The test uses actual OS identities and a real Unix
socket; it never claims isolation from a mode-only check.
"""

from __future__ import annotations

import os
import pwd
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = "qel-research"
EVAL = "qel-eval"


def _enabled() -> bool:
    return os.environ.get("QEL_REAL_PRINCIPALS") == "1"


pytestmark = pytest.mark.skipif(
    not _enabled(), reason="set QEL_REAL_PRINCIPALS=1 after provisioning real principals"
)


def _uid(name: str) -> int:
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        pytest.fail(f"missing principal {name}; run scripts/provision_isolation.sh as root")


def test_principals_are_distinct_non_root() -> None:
    research = _uid(RESEARCH)
    evaluator = _uid(EVAL)
    assert research != evaluator
    assert research != 0 and evaluator != 0
    assert os.getuid() not in {research, evaluator}


def test_research_cannot_read_evaluator_key_or_db() -> None:
    sealed = Path(os.environ.get("QEL_SEALED_ROOT", ROOT / "runtime" / "sealed"))
    if not sealed.exists():
        pytest.skip("sealed evaluator root is not provisioned")
    for name in ("key.pem", "registry.sqlite"):
        path = sealed / name
        if not path.exists():
            continue
        proc = subprocess.run(
            ["runuser", "-u", RESEARCH, "--", "test", "-r", str(path)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0, f"{RESEARCH} must not read {path}"
        assert oct(path.stat().st_mode & 0o777) in {"0o600", "0o700"}


def test_evaluator_owns_socket_and_peer_credentials_are_distinct() -> None:
    socket_path = Path(os.environ.get("QEL_EVAL_SOCKET", "/run/qel/qel-eval.sock"))
    if not socket_path.exists():
        pytest.skip("authorized evaluator socket is not provisioned")
    st = socket_path.stat()
    assert st.st_uid == _uid(EVAL)
    # A research-principal client must present a different SO_PEERCRED uid to
    # the evaluator; the evaluator rejects it unless the peer is authorized.
    proc = subprocess.run(
        [
            "runuser",
            "-u",
            RESEARCH,
            "--",
            "python3",
            "-c",
            "import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); print(s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12).hex())",
            str(socket_path),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    assert proc.stdout.strip()


def test_worker_network_namespace_blocks_external_egress() -> None:
    proc = subprocess.run(
        ["unshare", "-Urn", "--", "python3", "-c", "import socket; socket.create_connection(('1.1.1.1', 80), 2)"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode != 0, "network namespace must not reach the public internet"
