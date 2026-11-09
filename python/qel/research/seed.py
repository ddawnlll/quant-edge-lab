"""Frozen HMAC-SHA256 counter-mode seed protocol (``HMAC_SHA256_COUNTER_V1``).

The research layer may not use Python's ``random`` module or NumPy's PCG64 for
any draw that can reach an execution or validation report.  This module is the
single deterministic source of randomness for the seed protocol:

* every draw is labelled by the six frozen labels
  ``[GENERATOR_ID, PROGRAM_REPLICATION, VARIANT, COIN, TIME,
  CONTROL_REPLICATION]``;
* a 32-byte domain key is frozen in this file (``KEY_HEX``);
* the label tuple is encoded unambiguously and expanded with
  ``HMAC-SHA256(key, tag || labels || counter)`` in counter mode;
* each 64-bit block word contributes a 53-bit uniform in ``[0, 1)``;
* normal and standardized Student-t5 draws use the pinned SciPy inverse CDF
  when SciPy is importable and a deterministic stdlib fallback otherwise.

Nothing here depends on process state, clock state or hash randomisation.  The
published ``TEST_VECTORS`` pin the composition; ``protocol_hash()`` commits to
the key, label order, encoding, extraction rule and vectors.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import struct
import sys
from typing import Iterable, Mapping

PROTOCOL = "HMAC_SHA256_COUNTER_V1"

#: The six seed labels, in frozen order.  A label mapping must contain exactly
#: these keys; insertion order is irrelevant because encoding is ordered.
LABELS: tuple[str, ...] = (
    "GENERATOR_ID",
    "PROGRAM_REPLICATION",
    "VARIANT",
    "COIN",
    "TIME",
    "CONTROL_REPLICATION",
)

#: Frozen 32-byte domain key.  Derived once from
#: ``sha256(b"QEL/HMAC_SHA256_COUNTER_V1/FROZEN_KEY/2026-11-09")`` and pinned.
KEY_HEX = "aaf3d6e9d3d8ae4faff446eeace3d283817d882a138574c030334ccbd31f11e8"
_KEY = bytes.fromhex(KEY_HEX)

_MESSAGE_TAG = b"QEL|HMAC_SHA256_COUNTER_V1|U|"
_COUNTER_TAG = b"\x1e"
_UNIFORM_SCALE = 2.0**-53
_U64_MAX = (1 << 64) - 1
_COUNTER_MAX = (1 << 64) - 1

#: Smallest/largest uniforms used for inverse-CDF calls.  ``u = 0`` would map to
#: ``-inf``; clamping to the nearest representable 53-bit grid value keeps the
#: draw finite and reproducible without changing the uniform protocol.
U_MIN = _UNIFORM_SCALE
U_MAX = 1.0 - _UNIFORM_SCALE


class SeedError(ValueError):
    """A seed-protocol input is outside its frozen domain."""


class SeedBackendUnavailable(RuntimeError):
    """Inverse-CDF backend is unavailable and no fallback exists."""

    code = "BLOCKED_BACKEND"


def _encode_labels(labels: Mapping[str, int]) -> bytes:
    if not isinstance(labels, Mapping):
        raise SeedError("SEED_LABELS_NOT_A_MAPPING")
    if set(labels) != set(LABELS):
        missing = sorted(set(LABELS) - set(labels))
        extra = sorted(set(labels) - set(LABELS))
        raise SeedError(f"SEED_LABEL_SET missing={missing} extra={extra}")
    parts: list[bytes] = []
    for name in LABELS:
        value = labels[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise SeedError(f"SEED_LABEL_NOT_INT {name}={value!r}")
        if value < 0 or value > _U64_MAX:
            raise SeedError(f"SEED_LABEL_OUT_OF_RANGE {name}={value}")
        parts.append(name.encode("ascii") + b"\x00" + struct.pack(">Q", value))
    return b"\x1f".join(parts)


def _message(labels: Mapping[str, int], counter: int) -> bytes:
    if isinstance(counter, bool) or not isinstance(counter, int):
        raise SeedError("SEED_COUNTER_NOT_INT")
    if counter < 0 or counter > _COUNTER_MAX:
        raise SeedError("SEED_COUNTER_OUT_OF_RANGE")
    return _MESSAGE_TAG + _encode_labels(labels) + _COUNTER_TAG + struct.pack(">Q", counter)


def hmac_sha256(message: bytes, key: bytes = _KEY) -> bytes:
    """Raw HMAC-SHA256 primitive (exposed for RFC 4231 conformance tests)."""
    return hmac.new(key, message, hashlib.sha256).digest()


def seed_material(labels: Mapping[str, int]) -> bytes:
    """Return the canonical label encoding (without the counter)."""
    return _encode_labels(labels)


def counter_block(labels: Mapping[str, int], counter: int) -> bytes:
    """Return one 32-byte counter-mode block."""
    return hmac_sha256(_message(labels, counter))


def uniform_at(labels: Mapping[str, int], index: int) -> float:
    """Return uniform ``index`` in ``[0, 1)`` for a frozen label tuple.

    Four 53-bit uniforms are extracted from each 32-byte counter block; the
    block counter is ``index // 4``.  This is the only uniform mapping used by
    the research controls, the L1 rescue rule and the calibration generators.
    """
    if isinstance(index, bool) or not isinstance(index, int):
        raise SeedError("SEED_INDEX_NOT_INT")
    if index < 0:
        raise SeedError("SEED_INDEX_NEGATIVE")
    counter, slot = divmod(index, 4)
    block = counter_block(labels, counter)
    word = int.from_bytes(block[slot * 8 : (slot + 1) * 8], "big")
    # Take the high 53 bits so the result is exactly representable and strictly
    # below 1.0.
    return (word >> 11) * _UNIFORM_SCALE


def uniforms(labels: Mapping[str, int], count: int, start: int = 0) -> list[float]:
    """Return ``count`` consecutive uniforms from one label tuple."""
    if count < 0:
        raise SeedError("SEED_COUNT_NEGATIVE")
    return [uniform_at(labels, start + i) for i in range(count)]


def uniform_series(
    labels: Mapping[str, int], times: Iterable[int], start_index: int = 0
) -> list[float]:
    """Draw one uniform per ``TIME`` label value.

    ``TIME`` is replaced by each value in ``times``; the counter starts at
    ``start_index``.  This is the series form used by the generator law, where
    ``TIME`` is the simulation step.
    """
    return [uniform_at({**labels, "TIME": int(t)}, start_index) for t in times]


# ---------------------------------------------------------------------------
# Inverse-CDF backends
# ---------------------------------------------------------------------------

try:  # pragma: no cover - import exercised on every interpreter
    from scipy.stats import norm as _scipy_norm
    from scipy.stats import t as _scipy_t
except Exception:  # pragma: no cover - depends on the environment
    _scipy_norm = None
    _scipy_t = None

if _scipy_norm is not None and _scipy_t is not None:
    _NORMAL_BACKEND = "SCIPY_NORM_PPF"
    _T5_BACKEND = "SCIPY_T_PPF_DF5"
else:  # pragma: no cover - fallback path
    _NORMAL_BACKEND = "STDLIB_NORMALDIST_INV_CDF"
    _T5_BACKEND = "CLOSED_FORM_DF5_BISECTION"


def _clamp(u: float) -> float:
    if not isinstance(u, float) or math.isnan(u) or not (0.0 <= u < 1.0):
        raise SeedError(f"UNIFORM_OUT_OF_DOMAIN {u!r}")
    return min(max(u, U_MIN), U_MAX)


def _normal_ppf_stdlib(u: float) -> float:
    from statistics import NormalDist

    return NormalDist().inv_cdf(u)


def _t5_cdf_stdlib(t: float) -> float:
    """Closed-form Student-t CDF for five degrees of freedom."""
    x = t / math.sqrt(5.0)
    one_plus = 1.0 + x * x
    return 0.5 + (
        math.atan(x) + x * (5.0 + 3.0 * x * x) / (3.0 * one_plus * one_plus)
    ) / math.pi


def _t5_ppf_stdlib(u: float) -> float:
    """Deterministic bisection inverse of the closed-form t5 CDF."""
    lo, hi = -1.0e6, 1.0e6
    for _ in range(160):
        mid = 0.5 * (lo + hi)
        if _t5_cdf_stdlib(mid) < u:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def normal_at(labels: Mapping[str, int], index: int = 0) -> float:
    """Standard normal draw from uniform ``index``."""
    u = _clamp(uniform_at(labels, index))
    if _scipy_norm is not None:
        return float(_scipy_norm.ppf(u))
    return _normal_ppf_stdlib(u)


def t5_at(labels: Mapping[str, int], index: int = 0, standardized: bool = True) -> float:
    """Student-t (df=5) draw from uniform ``index``.

    With ``standardized=True`` (the generator-law convention) the draw is
    multiplied by ``sqrt(3/5)`` so it has unit variance.
    """
    u = _clamp(uniform_at(labels, index))
    if _scipy_t is not None:
        value = float(_scipy_t.ppf(u, 5))
    else:
        value = _t5_ppf_stdlib(u)
    if standardized:
        value *= math.sqrt(3.0 / 5.0)
    return value


def backend_info() -> dict:
    """Return the frozen backend identity recorded in reports."""
    return {
        "protocol": PROTOCOL,
        "normal_backend": _NORMAL_BACKEND,
        "t5_backend": _T5_BACKEND,
        "backend_hash": backend_hash(),
        "scipy_available": _scipy_norm is not None and _scipy_t is not None,
        "python": sys.version.split()[0],
    }


# ---------------------------------------------------------------------------
# Published vectors
# ---------------------------------------------------------------------------

#: Label tuples -> expected values.  Values were produced by this frozen
#: implementation and are asserted in tests/test_controls.py.  ``u8`` is the
#: first eight uniforms for ``index=0..7`` (two counter blocks); ``normal`` and
#: ``t5`` are the inverse-CDF draws for ``index=0``.
TEST_VECTORS: tuple[dict, ...] = (
    {
        "name": "CONTROL_ENTRY_SLOT_0",
        "labels": {
            "GENERATOR_ID": 4865,
            "PROGRAM_REPLICATION": 7,
            "VARIANT": 0,
            "COIN": 0,
            "TIME": 0,
            "CONTROL_REPLICATION": 0,
        },
        "u8": [
            0.36258670815313065,
            0.4337616821079239,
            0.1848186171390337,
            0.5909583855822466,
            0.1095576928650911,
            0.19002993263861934,
            0.30431499224186076,
            0.8731729500375687,
        ],
        "normal": -0.3515531359719028,
        "t5": -0.2881107277541506,
    },
    {
        "name": "CONTROL_SIDE_0",
        "labels": {
            "GENERATOR_ID": 4866,
            "PROGRAM_REPLICATION": 7,
            "VARIANT": 0,
            "COIN": 0,
            "TIME": 0,
            "CONTROL_REPLICATION": 0,
        },
        "u8": [
            0.17837147698751776,
            0.02036516497780383,
            0.7499031651070689,
            0.7739407045491817,
            0.14613099310747535,
            0.39737372259159665,
            0.21275488358415529,
            0.6162614031397242,
        ],
        "normal": -0.9215890850710922,
        "t5": -0.7860965424448934,
    },
    {
        "name": "CALIBRATION_NORMAL_T5",
        "labels": {
            "GENERATOR_ID": 9001,
            "PROGRAM_REPLICATION": 0,
            "VARIANT": 3,
            "COIN": 2,
            "TIME": 17,
            "CONTROL_REPLICATION": 0,
        },
        "u8": [
            0.7642616912282961,
            0.0701389690540366,
            0.7651202700571,
            0.6534280512809186,
            0.29670134405469006,
            0.5747584093743388,
            0.33220005534034636,
            0.712834180829503,
        ],
        "normal": 0.7200785759063778,
        "t5": 0.6030442194479999,
    },
)


def _vectors_payload() -> str:
    return json.dumps(TEST_VECTORS, sort_keys=True, separators=(",", ":"))


def _uniform_vectors_payload() -> str:
    """Uniform-only vector payload; stable across inverse-CDF backends."""
    return json.dumps(
        [{"name": v["name"], "labels": v["labels"], "u8": v["u8"]} for v in TEST_VECTORS],
        sort_keys=True,
        separators=(",", ":"),
    )


def protocol_hash() -> str:
    """Commitment to key, labels, encoding, extraction and uniform vectors.

    The inverse-CDF backend is deliberately excluded: the uniform seed protocol
    is frozen, while the numeric backend is recorded separately by
    :func:`backend_hash`.
    """
    descriptor = "|".join(
        [
            PROTOCOL,
            "key=" + KEY_HEX,
            "labels=" + ",".join(LABELS),
            "tag=" + _MESSAGE_TAG.hex(),
            "counter_tag=" + _COUNTER_TAG.hex(),
            "uniform=(u64_be>>11)*2^-53",
            "block=32B,4uniforms,big_endian_words",
            "vectors=" + _uniform_vectors_payload(),
        ]
    )
    return hashlib.sha256(descriptor.encode("utf-8")).hexdigest()


#: Import-time constant used by reports; deterministic across processes.
PROTOCOL_HASH = protocol_hash()


def backend_hash() -> str:
    """Commitment to the inverse-CDF backend and its published draw vectors."""
    descriptor = "|".join(
        [
            _NORMAL_BACKEND,
            _T5_BACKEND,
            "t5_standardization=sqrt(3/5)",
            _vectors_payload(),
        ]
    )
    return hashlib.sha256(descriptor.encode("utf-8")).hexdigest()


BACKEND_HASH = backend_hash()


def self_test() -> dict:
    """Verify every published vector and return a machine-readable receipt."""
    failures = []
    for vector in TEST_VECTORS:
        labels = vector["labels"]
        got_u = uniforms(labels, 8)
        if any(abs(a - b) > 1e-15 for a, b in zip(got_u, vector["u8"])):
            failures.append({"vector": vector["name"], "field": "u8"})
        if abs(normal_at(labels, 0) - vector["normal"]) > 1e-12:
            failures.append({"vector": vector["name"], "field": "normal"})
        if abs(t5_at(labels, 0) - vector["t5"]) > 1e-12:
            failures.append({"vector": vector["name"], "field": "t5"})
    return {
        "protocol": PROTOCOL,
        "protocol_hash": PROTOCOL_HASH,
        "vectors": len(TEST_VECTORS),
        "backend": backend_info(),
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
    }
