import pytest

from qel.accounting.core import AccountingError, dec, reconstruct


def test_f23_exact_identity():
    tape = [
        {"type": "SET_WALLET", "cash": "1000"},
        {"type": "ALLOCATE_ISOLATED", "amount": "300"},
        {"type": "FILL", "side": "BUY", "qty": "2", "price": "100", "fee": "0.2"},
        {"type": "FILL", "side": "SELL", "qty": "1", "price": "110", "fee": "0.11"},
        {"type": "FUNDING", "amount": "-0.5", "epoch": "1"},
        {"type": "MARK", "price": "105"},
    ]
    out = reconstruct(tape)
    assert out["free_cash"] == "700"
    assert out["isolated_cash"] == "309.19"
    assert out["price_realized"] == "10"
    assert out["fee_total"] == "0.31"
    assert out["unrealized"] == "5"
    assert out["total_cash"] == "1009.19"
    assert out["equity"] == "1014.19"
    assert out["unexplained_residual"] == "0"
    assert out["postings_balanced"]
    assert out["fifo_realized"] == "10"


def test_f08_funding_and_equity():
    tape = [
        {"type": "SET_WALLET", "cash": "1000"},
        {"type": "SET_POSITION", "qty": "2", "entry": "100", "epoch": "1"},
        {"type": "FUNDING", "amount": "-2", "epoch": "1"},
        {"type": "MARK", "price": "100"},
    ]
    out = reconstruct(tape)
    assert out["funding_cash"] == "-2"
    assert out["equity"] == "998"
    assert out["unexplained_residual"] == "0"


def test_f16_reservations_do_not_double_count():
    tape = [
        {"type": "SET_WALLET", "cash": "20"},
        {"type": "RESERVE", "id": "O-A", "margin": "12"},
        {"type": "RESERVE", "id": "O-B", "margin": "12"},
    ]
    out = reconstruct(tape)
    assert out["free_after_reservations"] == "8"
    assert out["negative_free_cash"] is False


def test_f24_terminal_close_cost():
    tape = [
        {"type": "SET_WALLET", "cash": "1000"},
        {"type": "SET_POSITION", "qty": "1", "entry": "100", "epoch": "1"},
        {"type": "MARK", "price": "105"},
        {"type": "END_FOLD", "close_cost": "0.2"},
    ]
    out = reconstruct(tape)
    assert out["endpoint_equity"] == "1004.8"
    assert out["equity"] == "1005"
    assert out["unexplained_residual"] == "0"


@pytest.mark.parametrize(
    "tape, expected_code",
    [
        ([{"type": "SET_WALLET", "cash": "20"}, {"type": "ALLOCATE_ISOLATED", "amount": "21"}], "INFEASIBLE_CAPITAL"),
        ([{"type": "FILL", "side": "BUY", "qty": "1", "price": "100", "fee": "0"}], None),
    ],
)
def test_negative_paths(tape, expected_code):
    if expected_code:
        with pytest.raises(AccountingError):
            reconstruct(tape)
    else:
        # no wallet/position is a valid zero-capital reconstruction with no hidden cash
        out = reconstruct(tape)
        assert out["negative_free_cash"] is False


def test_fee_sign_mutation_is_visible():
    base = reconstruct([
        {"type": "SET_WALLET", "cash": "100"},
        {"type": "FILL", "side": "BUY", "qty": "1", "price": "100", "fee": "0.5"},
        {"type": "MARK", "price": "100"},
    ])
    mutated = reconstruct([
        {"type": "SET_WALLET", "cash": "100"},
        {"type": "FILL", "side": "BUY", "qty": "1", "price": "100", "fee": "-0.5"},
        {"type": "MARK", "price": "100"},
    ])
    assert dec(mutated["equity"]) - dec(base["equity"]) == 1


def test_funding_double_count_is_rejected():
    tape = [
        {"type": "SET_WALLET", "cash": "1000"},
        {"type": "SET_POSITION", "qty": "2", "entry": "100", "epoch": "1"},
        {"type": "FUNDING", "amount": "-2", "epoch": "1", "settlement_id": "SET-1"},
        {"type": "FUNDING", "amount": "-2", "epoch": "1", "settlement_id": "SET-1"},
        {"type": "MARK", "price": "100"},
    ]
    with pytest.raises(AccountingError, match="duplicate funding"):
        reconstruct(tape)
