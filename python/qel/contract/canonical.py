"""Canonical JSON identity per QEL (RFC 8785 JCS + NFC validation).

No floating point is accepted in configuration, money or identity objects.
Feature-state floats are validated separately by the policy engine.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation

JCS_MAX_SAFE = 9007199254740991

_DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]*[1-9])?$")
_UINT_RE = re.compile(r"^(0|[1-9][0-9]*)$")
_INT_RE = re.compile(r"^-?(0|[1-9][0-9]*)$")


class CanonicalError(ValueError):
    """Input is outside the canonical identity domain."""


def _check_text(text: str) -> str:
    if not isinstance(text, str):
        raise CanonicalError("text must be a string")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalError("lone surrogate in text") from exc
    if unicodedata.normalize("NFC", text) != text:
        raise CanonicalError("non-NFC text")
    return text


def _utf16_key(text: str) -> bytes:
    return text.encode("utf-16-be", "surrogatepass")


def _number(value) -> str:
    if isinstance(value, bool):
        raise CanonicalError("boolean is not a number")
    if isinstance(value, int):
        if abs(value) > JCS_MAX_SAFE:
            raise CanonicalError("integer outside JCS safe range")
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalError("nonfinite number")
        if value == 0.0:
            return "0"
        # Python repr is shortest round-trip; normalise ECMAScript exponent form.
        text = repr(value)
        if "e" not in text and "E" not in text:
            return text
        mantissa, exp = re.split("[eE]", text)
        exp_i = int(exp)
        if "." in mantissa:
            whole, frac = mantissa.split(".")
        else:
            whole, frac = mantissa, ""
        digits = (whole + frac).lstrip("-")
        sign = "-" if mantissa.startswith("-") else ""
        digits = digits.lstrip("0") or "0"
        # ECMAScript: decimal notation for 1e-6 <= |x| < 1e21.
        adjusted = exp_i + len(whole) - 1
        if -7 < adjusted < 21:
            if exp_i >= 0:
                out = digits + "0" * exp_i
                if exp_i >= len(frac):
                    out = whole + frac + "0" * (exp_i - len(frac))
                else:
                    out = whole + frac[:exp_i] + "." + frac[exp_i:]
                return sign + out
            pos = len(whole) + exp_i
            if pos > 0:
                return sign + whole[:pos] + "." + whole[pos:]
            return sign + "0." + "0" * (-pos) + whole
        mantissa_out = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        return f"{sign}{mantissa_out}e{adjusted:+d}"
    raise CanonicalError("unsupported number type")


def canonical_bytes(value) -> bytes:
    """Serialize a JSON value to RFC 8785 canonical UTF-8 bytes."""

    def emit(v) -> str:
        if v is None:
            return "null"
        if v is True:
            return "true"
        if v is False:
            return "false"
        if isinstance(v, str):
            _check_text(v)
            # json.dumps with ensure_ascii=False escapes control characters and
            # quotes exactly as JCS requires for BMP text.
            import json

            return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        if isinstance(v, (int, float)):
            return _number(v)
        if isinstance(v, (list, tuple)):
            return "[" + ",".join(emit(x) for x in v) + "]"
        if isinstance(v, dict):
            items = []
            for k in sorted(v.keys(), key=_utf16_key):
                if not isinstance(k, str):
                    raise CanonicalError("object key is not a string")
                _check_text(k)
                items.append(emit(k) + ":" + emit(v[k]))
            return "{" + ",".join(items) + "}"
        raise CanonicalError(f"unsupported type {type(v).__name__}")

    return emit(value).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_jcs(value) -> str:
    return sha256_hex(canonical_bytes(value))


def domain_hash(prefix: str, value) -> str:
    return sha256_hex(prefix.encode("utf-8") + canonical_bytes(value))


# ---------------------------------------------------------------- decimals ---


def parse_decimal(text: str) -> tuple[int, int]:
    """Return (coefficient, scale) for a canonical decimal string."""
    if not isinstance(text, str) or not _DECIMAL_RE.match(text):
        raise CanonicalError(f"non-canonical decimal: {text!r}")
    if text.startswith("-0") and text in ("-0",):
        raise CanonicalError("negative zero")
    if text.startswith("-0.") and int(text.split(".")[1]) == 0:
        raise CanonicalError("negative zero")
    neg = text.startswith("-")
    body = text[1:] if neg else text
    if "." in body:
        whole, frac = body.split(".")
        scale = len(frac)
        coeff = int(whole) * (10 ** scale) + int(frac)
    else:
        coeff = int(body)
        scale = 0
    return (-coeff if neg else coeff), scale


def format_decimal(coeff: int, scale: int) -> str:
    if scale == 0:
        return str(coeff)
    sign = "-" if coeff < 0 else ""
    digits = str(abs(coeff)).rjust(scale + 1, "0")
    whole, frac = digits[:-scale], digits[-scale:]
    frac = frac.rstrip("0")
    return f"{sign}{whole}" + (f".{frac}" if frac else "")


def rescale(coeff: int, scale: int, target: int) -> int:
    if target < scale:
        factor = 10 ** (scale - target)
        if coeff % factor != 0:
            raise CanonicalError("lossy rescale")
        return coeff // factor
    return coeff * (10 ** (target - scale))


def decimal_add(a: str, b: str) -> str:
    ca, sa = parse_decimal(a)
    cb, sb = parse_decimal(b)
    s = max(sa, sb)
    return format_decimal(rescale(ca, sa, s) + rescale(cb, sb, s), s)


def decimal_mul(a: str, b: str) -> str:
    ca, sa = parse_decimal(a)
    cb, sb = parse_decimal(b)
    return format_decimal(ca * cb, sa + sb)


def decimal_mul_rational(a: str, num: str, den: str) -> str:
    """Exact a*num/den with truncation toward zero when the result has more
    fractional digits than a. All arguments are canonical decimals."""
    ca, sa = parse_decimal(a)
    cn, _ = parse_decimal(num)
    cd, _ = parse_decimal(den)
    if cd == 0:
        raise CanonicalError("zero denominator")
    # coeff = ca * cn / cd at scale sa
    num_total = ca * cn
    den_total = cd
    # Scale up to keep integer precision then truncate toward zero.
    return format_decimal(_trunc_div(num_total, den_total), sa)


def _trunc_div(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


# ------------------------------------------------------------- integer domain


def parse_uint(text: str, maximum: int = (1 << 64) - 1) -> int:
    if not isinstance(text, str) or not _UINT_RE.match(text):
        raise CanonicalError(f"non-canonical unsigned integer: {text!r}")
    value = int(text)
    if value > maximum:
        raise CanonicalError("unsigned integer overflow")
    return value


def parse_int128(text: str, minimum: int = -(1 << 127), maximum: int = (1 << 127) - 1) -> int:
    if not isinstance(text, str) or not _INT_RE.match(text):
        raise CanonicalError(f"non-canonical integer: {text!r}")
    if text == "-0":
        raise CanonicalError("negative zero")
    value = int(text)
    if not minimum <= value <= maximum:
        raise CanonicalError("integer outside declared domain")
    return value


def checked_mul(values: list[int], bits: int) -> int:
    lo = -(1 << (bits - 1))
    hi = (1 << (bits - 1)) - 1
    acc = 1
    for v in values:
        if not lo <= v <= hi:
            raise CanonicalError("operand outside domain")
        if v != 0 and (acc > hi // v if v > 0 else acc < hi // v if acc > 0 else False):
            raise CanonicalError("NUMERIC_OVERFLOW")
        acc *= v
        if acc < lo or acc > hi:
            raise CanonicalError("NUMERIC_OVERFLOW")
    return acc
