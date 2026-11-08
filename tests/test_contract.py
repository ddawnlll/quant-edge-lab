import json

import pytest

from qel.contract import (
    CanonicalError,
    canonical_bytes,
    decimal_add,
    decimal_mul,
    hash_jcs,
    parse_decimal,
    parse_int128,
    parse_uint,
    validate_def,
)
from qel.contract.runkey import RUN_KEY_OBJECT_KEYS, run_key


def test_f21_canonical_vector():
    value = {"a": "9007199254740993", "b": "1.25", "c": None}
    assert canonical_bytes(value).decode() == '{"a":"9007199254740993","b":"1.25","c":null}'
    assert hash_jcs(value) == "129712fa856798af9106eb65d951f98ef56459ae40d587552cd7a4b162b7ae6e"


def test_key_order_and_unicode_sorting():
    assert hash_jcs({"b": 1, "a": 2}) == hash_jcs({"a": 2, "b": 1})
    # JCS orders by UTF-16 code units: the surrogate pair D83D DE00 sorts
    # before the BMP character E000 (unlike Unicode code-point order).
    assert canonical_bytes({"\U0001f600": 1, "\ue000": 2}) == '{"\U0001f600":1,"\ue000":2}'.encode()


@pytest.mark.parametrize("text", ["-0", "01", "1e3", "1.230", "1."])
def test_noncanonical_decimals_rejected(text):
    with pytest.raises(CanonicalError):
        parse_decimal(text)


def test_decimal_arithmetic_is_exact():
    assert decimal_add("0.1", "0.2") == "0.3"
    assert decimal_mul("1.25", "-0.02") == "-0.025"


def test_integer_domains():
    assert parse_uint("18446744073709551615") == (1 << 64) - 1
    with pytest.raises(CanonicalError):
        parse_uint("18446744073709551616")
    assert parse_int128("-170141183460469231731687303715884105728") < 0
    with pytest.raises(CanonicalError):
        parse_int128("170141183460469231731687303715884105728")


def test_non_nfc_and_lone_surrogate_rejected():
    with pytest.raises(CanonicalError):
        canonical_bytes("e\u0301")
    with pytest.raises(CanonicalError):
        canonical_bytes("\ud800")


def test_strategy_example_validates():
    from qel.contract import repo_root

    strategy = json.loads((repo_root() / "specification/examples/strategy_mr.json").read_text())
    validate_def("StrategySpec", strategy)


def test_strategy_overflow_rejected():
    from qel.contract import repo_root

    strategy = json.loads((repo_root() / "specification/examples/strategy_mr.json").read_text())
    strategy["max_holding_ns"] = "18446744073709551616"
    with pytest.raises(Exception):
        validate_def("StrategySpec", strategy)


def test_run_key_invariance_and_attempt_exclusion():
    material = {k: ("%064x" % (i + 1)) for i, k in enumerate(RUN_KEY_OBJECT_KEYS)}
    base = run_key(material)
    assert base == run_key(dict(reversed(list(material.items()))))
    assert base != run_key({**material, "strategy_hash": "f" * 64})
    with pytest.raises(ValueError):
        run_key({**material, "attempt_id": "a" * 64})
