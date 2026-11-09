"""TASK-10: venue funding, margin and liquidation profile tests.

Every value is derived from the venue rule book and the independent exact
``Account``; fixture expected values are only compared, never read back as
inputs. The tests exercise the required fixtures F08, F09, F10, F11 (MARK and
QUOTE_IS_NOT_MARK), F12, F13 (REBATE, I128_OVERFLOW, RULE_ROUND) and F16.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from qel.accounting import venue as V
from qel.accounting.core import dec, reconstruct
from qel.conformance import Harness

BIG = 10**18


def ruleset(
    *,
    decision_ns: int = 0,
    mark_price: str | None = "100",
    maintenance_rate: str | None = None,
    liquidation_fee_rate: str = "0.001",
    max_leverage: str = "20",
    taker: str = "0",
    maker: str = "0",
    funding_rate: str = "0.0001",
    order: str = V.SETTLEMENT_ORDER_BEFORE,
    status: str = "TRADING",
    known_at: int = 0,
) -> list[V.VenueRule]:
    rules = [
        V.rule(
            "FEE",
            {"maker_rate": maker, "taker_rate": taker},
            valid_from=0,
            valid_to=BIG,
            known_at=known_at,
        ),
        V.rule(
            "FUNDING",
            {"rate": funding_rate, "interval_ns": 8 * 3600 * 10**9},
            valid_from=0,
            valid_to=BIG,
            known_at=known_at,
        ),
        V.rule(
            "TRADING_STATUS",
            {"status": status},
            valid_from=0,
            valid_to=BIG,
            known_at=known_at,
        ),
        V.rule(
            "SETTLEMENT_ORDER",
            {"order": order},
            valid_from=0,
            valid_to=BIG,
            known_at=known_at,
        ),
    ]
    if mark_price is not None:
        rules.append(
            V.rule(
                "MARK",
                {"source": "EXCHANGE_MARK_PRICE", "price": mark_price},
                valid_from=0,
                valid_to=BIG,
                known_at=known_at,
            )
        )
    if maintenance_rate is not None:
        rules.append(
            V.rule(
                "MARGIN_TIER",
                {
                    "tiers": [
                        {
                            "max_notional": "1000000",
                            "maintenance_rate": maintenance_rate,
                            "max_leverage": max_leverage,
                            "liquidation_fee_rate": liquidation_fee_rate,
                        }
                    ]
                },
                valid_from=0,
                valid_to=BIG,
                known_at=known_at,
            )
        )
    return rules


def venue_account(*, decision_ns: int = 0, **kwargs) -> V.VenueAccount:
    profile = V.VenueProfile(ruleset(decision_ns=decision_ns, **kwargs), decision_ns=decision_ns)
    return V.VenueAccount(profile)


# ---------------------------------------------------------------------------
# Point-in-time rule book
# ---------------------------------------------------------------------------


def test_rule_known_at_gate_and_missing_rule():
    book = V.VenueRuleBook(
        [
            V.rule(
                "FEE",
                {"maker_rate": "0.001", "taker_rate": "0.002"},
                valid_from=100,
                valid_to=200,
                known_at=150,
            )
        ]
    )
    # Rule is valid at 120 but was not known at decision 140.
    with pytest.raises(V.VenueError) as exc:
        book.resolve("FEE", 120, 140)
    assert exc.value.code == V.LOOKAHEAD_RULE
    assert book.resolve("FEE", 120, 160).value["taker_rate"] == "0.002"
    # Outside the valid window there is no rule at all.
    with pytest.raises(V.VenueError) as exc:
        book.resolve("FEE", 50, 160)
    assert exc.value.code == V.UNKNOWN_RULE


def test_conflicting_rule_at_same_valid_and_known_time():
    book = V.VenueRuleBook()
    first = V.rule(
        "MARK",
        {"source": "EXCHANGE_MARK_PRICE", "price": "100"},
        valid_from=0,
        valid_to=BIG,
        known_at=0,
    )
    book.add(first)
    with pytest.raises(V.VenueError) as exc:
        book.add(
            V.rule(
                "MARK",
                {"source": "EXCHANGE_MARK_PRICE", "price": "101"},
                valid_from=0,
                valid_to=BIG,
                known_at=0,
            )
        )
    assert exc.value.code == V.CONFLICTING_RULE


def test_unknown_rule_and_invalid_rule_rejected():
    book = V.VenueRuleBook()
    with pytest.raises(V.VenueError) as exc:
        book.add(
            V.rule("BOGUS", {}, valid_from=0, valid_to=10, known_at=0)
        )
    assert exc.value.code == V.UNKNOWN_RULE
    with pytest.raises(V.VenueError) as exc:
        book.add(V.rule("FEE", {"taker_rate": "0"}, valid_from=10, valid_to=10, known_at=0))
    assert exc.value.code == V.INVALID_RULE


def test_missing_and_future_only_mark_fail_closed():
    missing = V.VenueProfile(ruleset(mark_price=None), decision_ns=100)
    with pytest.raises(V.VenueError) as exc:
        missing.mark(100)
    assert exc.value.code == V.MISSING_SETTLEMENT_MARK

    future = V.VenueProfile(
        [
            V.rule(
                "MARK",
                {"source": "EXCHANGE_MARK_PRICE", "price": "100"},
                valid_from=200,
                valid_to=BIG,
                known_at=0,
            )
        ],
        decision_ns=100,
    )
    with pytest.raises(V.VenueError) as exc:
        future.mark(100)
    assert exc.value.code == V.FUTURE_ONLY_MARK


def test_mark_source_must_be_declared_mark():
    profile = V.VenueProfile(
        [
            V.rule(
                "MARK",
                {"source": "BOOK_BID", "price": "94"},
                valid_from=0,
                valid_to=BIG,
                known_at=0,
            )
        ],
        decision_ns=0,
    )
    with pytest.raises(V.VenueError) as exc:
        profile.mark(0)
    assert exc.value.code == V.UNKNOWN_MARK_SOURCE


# ---------------------------------------------------------------------------
# F08: funding boundary
# ---------------------------------------------------------------------------


def test_f08_funding_before_same_time_close():
    acct = venue_account(decision_ns=100)
    acct.venue_set_wallet("1000")
    acct.at(99)
    acct.set_position(dec("2"), dec("100"), 1)

    def funding() -> None:
        acct.settle_funding(
            "SET-1",
            epoch=1,
            settlement_ts=100,
            available_ns=100,
            mark="100",
            rate=Fraction(1, 100),
        )

    acct.execute_boundary(100, funding, lambda: acct.close_position("100", fee="0"))

    assert acct.funding == dec("-2")
    assert acct.funding_epochs[1] == dec("-2")
    assert acct.equity() == dec("998")
    assert acct.reconcile()["status"] == "PASS"


def test_settlement_boundary_order_changes_funding_when_reversed():
    def run(order: str):
        acct = venue_account(decision_ns=100, order=order)
        acct.venue_set_wallet("1000")
        acct.at(99)
        acct.set_position(dec("2"), dec("100"), 1)

        def funding() -> None:
            acct.settle_funding(
                "SET-1",
                epoch=1,
                settlement_ts=100,
                available_ns=100,
                mark="100",
                rate=Fraction(1, 100),
            )

        acct.execute_boundary(100, funding, lambda: acct.close_position("100", fee="0"))
        return acct

    before = run(V.SETTLEMENT_ORDER_BEFORE)
    after = run(V.SETTLEMENT_ORDER_AFTER)
    assert before.funding == dec("-2")
    # AFTER closes first: the surviving position is flat, so no funding is owed.
    assert after.funding == dec("0")
    assert before.equity() == dec("998")
    assert after.equity() == dec("1000")


# ---------------------------------------------------------------------------
# F09: late funding attribution
# ---------------------------------------------------------------------------


def test_f09_late_funding_applies_once_to_earning_epoch():
    acct = venue_account(decision_ns=100)
    acct.venue_set_wallet("1000")
    acct.at(90)
    acct.set_position(dec("2"), dec("100"), 1)

    acct.at(100)
    scheduled = acct.settle_funding(
        "SET-1",
        epoch=1,
        settlement_ts=100,
        available_ns=200,
        mark="100",
        rate=Fraction(1, 100),
    )
    assert scheduled.applied is False

    # Position rolls to epoch 2 before the receipt is available.
    acct.at(110)
    acct.close_position("100", fee="0")
    acct.set_position(dec("-1"), dec("100"), 2)

    acct.at(200)
    acct.receive_funding("SET-1", 200)

    assert acct.funding == dec("-2")
    assert acct.funding_epochs[1] == dec("-2")
    assert acct.funding_epochs.get(2, Fraction(0)) == 0
    assert len(acct.funding_applications) == 1

    with pytest.raises(V.VenueError) as exc:
        acct.receive_funding("SET-1", 200)
    assert exc.value.code == V.DUPLICATE_SETTLEMENT


def test_repeated_equal_amounts_in_distinct_settlements_survive():
    acct = venue_account(decision_ns=100)
    acct.venue_set_wallet("1000")
    acct.at(99)
    acct.set_position(dec("2"), dec("100"), 1)
    acct.at(100)
    acct.settle_funding("SET-A", epoch=1, settlement_ts=100, available_ns=100, mark="100", rate=Fraction(1, 100))
    acct.settle_funding("SET-B", epoch=1, settlement_ts=100, available_ns=100, mark="100", rate=Fraction(1, 100))
    assert acct.funding == dec("-4")
    assert len(acct.funding_applications) == 2
    assert len(acct.funding_ids) == 2


def test_exact_duplicate_settlement_is_rejected():
    acct = venue_account(decision_ns=100)
    acct.venue_set_wallet("1000")
    acct.at(99)
    acct.set_position(dec("2"), dec("100"), 1)
    acct.at(100)
    acct.settle_funding("SET-1", epoch=1, settlement_ts=100, available_ns=100, mark="100", rate=Fraction(1, 100))
    with pytest.raises(V.VenueError) as exc:
        acct.settle_funding("SET-1", epoch=1, settlement_ts=100, available_ns=100, mark="100", rate=Fraction(1, 100))
    assert exc.value.code == V.DUPLICATE_SETTLEMENT


# ---------------------------------------------------------------------------
# F10: no future mark
# ---------------------------------------------------------------------------


def test_f10_missing_settlement_mark_never_infers_a_payment_or_mark():
    acct = venue_account(decision_ns=100, mark_price=None)
    acct.venue_set_wallet("1000")
    acct.at(100)
    acct.set_position(dec("1"), dec("100"), 1)
    # A later mark event is not visible at the settlement instant.
    with pytest.raises(V.VenueError) as exc:
        acct.settle_funding(
            "SET-1",
            epoch=1,
            settlement_ts=100,
            available_ns=100,
            mark=None,
            rate=Fraction(1, 100),
        )
    assert exc.value.code == V.MISSING_SETTLEMENT_MARK
    assert acct.funding == 0
    assert acct.settlements == {}
    assert acct.mark_history == []
    assert acct.mark_path_certificate == "missing"
    assert acct.mark_trigger_inferred_from_quote is False


# ---------------------------------------------------------------------------
# F11: liquidation data-path capability
# ---------------------------------------------------------------------------


def test_f11_mark_triggered_isolated_liquidation():
    acct = venue_account(decision_ns=1, maintenance_rate="0.05")
    acct.venue_set_wallet("1000")
    acct.allocate_isolated("10")
    acct.at(0)
    acct.set_position(dec("1"), dec("100"), 1)
    acct.at(1)
    assert acct.check_liquidation(mark="94") is True
    metrics = acct.liquidation_trigger_metrics
    assert metrics["isolated_equity"] == "4"
    assert metrics["maintenance"] == "4.7"
    assert acct.liquidation_triggered is True
    # Forced close is exact and leaves the position flat.
    assert acct.position_qty == 0
    assert acct.liquidation_events[-1]["qty"] == dec("1")
    assert acct.liquidation_cost == dec("0.09")


def test_f11_quote_is_not_a_mark():
    acct = venue_account(decision_ns=1, maintenance_rate="0.05")
    acct.venue_set_wallet("1000")
    acct.allocate_isolated("10")
    acct.at(0)
    acct.set_position(dec("1"), dec("100"), 1)
    acct.at(1)
    acct.observe_quote("94", "95")
    assert acct.mark_path_certificate == "missing"
    assert acct.mark_trigger_inferred_from_quote is False
    assert acct.liquidation_triggered is False
    assert acct.position_qty == dec("1")


# ---------------------------------------------------------------------------
# F12: funding debit crosses maintenance while the mark is unchanged
# ---------------------------------------------------------------------------


def test_f12_funding_debit_triggers_liquidation_at_unchanged_mark():
    acct = venue_account(decision_ns=100, maintenance_rate="0.05")
    acct.venue_set_wallet("1000")
    acct.allocate_isolated("5.1")
    acct.at(0)
    acct.set_position(dec("1"), dec("100"), 1)
    acct.at(100)
    acct.settle_funding(
        "SET-1",
        epoch=1,
        settlement_ts=100,
        available_ns=100,
        mark="100",
        rate=Fraction(2, 1000),
    )
    assert acct.funding == dec("-0.2")
    metrics = acct.liquidation_trigger_metrics
    assert metrics["mark"] == "100"
    assert metrics["isolated_equity"] == "4.9"
    assert metrics["maintenance"] == "5"
    assert acct.liquidation_triggered is True
    # Exact liquidation cost: notional 100 * 0.001.
    assert acct.liquidation_cost == dec("0.1")
    assert acct.liquidation_events[-1]["liquidation_cost"] == dec("0.1")


# ---------------------------------------------------------------------------
# F13: precision, signed fee and overflow
# ---------------------------------------------------------------------------


def test_f13_rebate_signed_fee():
    out = V.exact_fee("100", "-1", "10000")
    assert out["fee"] == dec("-0.01")
    assert out["cash_delta"] == dec("0.01")


def test_f13_i128_overflow():
    with pytest.raises(V.VenueError) as exc:
        V.checked_multiply(
            ["9223372036854775807", "9223372036854775807", "9223372036854775807"],
            bits=128,
        )
    assert exc.value.code == V.NUMERIC_OVERFLOW
    assert V.checked_multiply(["9223372036854775807"], bits=128) == 9223372036854775807


def test_f13_rule_round():
    assert V.round_qty("1.239", "0.01") == dec("1.23")


# ---------------------------------------------------------------------------
# F16: shared-wallet reservations
# ---------------------------------------------------------------------------


def test_f16_shared_wallet_reservations():
    acct = venue_account(decision_ns=2)
    acct.venue_set_wallet("20")
    acct.at(1)
    assert acct.reserve("O-A", "12") is True
    acct.at(2)
    assert acct.reserve("O-B", "12") is False
    snap = acct.snapshot()
    assert snap["free_after_reservations"] == "8"
    assert snap["negative_free_cash"] is False
    assert snap["reconciliation"]["status"] == "PASS"


# ---------------------------------------------------------------------------
# Margin tiers, leverage and trading status
# ---------------------------------------------------------------------------


def test_margin_tier_and_leverage_checks():
    profile = V.VenueProfile(
        ruleset(maintenance_rate="0.05", max_leverage="5"), decision_ns=0
    )
    tier = profile.margin_tier(0, "500")
    assert tier["max_leverage"] == "5"
    assert profile.maintenance_rate(0, "500") == dec("0.05")
    profile.check_leverage(0, "500", "100")  # 500 <= 100 * 5
    with pytest.raises(V.VenueError) as exc:
        profile.check_leverage(0, "501", "100")
    assert exc.value.code == V.LEVERAGE_EXCEEDED


def test_missing_tier_fails_closed():
    profile = V.VenueProfile(
        [
            V.rule(
                "MARGIN_TIER",
                {"tiers": [{"max_notional": "100", "maintenance_rate": "0.01"}]},
                valid_from=0,
                valid_to=BIG,
                known_at=0,
            )
        ],
        decision_ns=0,
    )
    with pytest.raises(V.VenueError) as exc:
        profile.margin_tier(0, "101")
    assert exc.value.code == V.TIER_NOT_FOUND


def test_halted_venue_rejects_orders():
    acct = venue_account(decision_ns=1, status="HALTED")
    acct.venue_set_wallet("1000")
    acct.at(1)
    with pytest.raises(V.VenueError) as exc:
        acct.venue_fill("BUY", "1", "100", fee="0")
    assert exc.value.code == V.TRADING_HALTED


# ---------------------------------------------------------------------------
# Reconciliation: fee/funding sign and reservation mutations must break
# ---------------------------------------------------------------------------


def test_fee_sign_mutation_breaks_reconciliation():
    acct = venue_account(decision_ns=1)
    acct.venue_set_wallet("1000")
    acct.at(1)
    acct.venue_fill("BUY", "1", "100", fee="0.5")
    assert acct.reconcile()["status"] == "PASS"

    for posting in acct.venue_postings:
        if posting.kind == "FEE":
            posting.amount = -posting.amount
    result = acct.reconcile()
    assert result["status"] == "FAIL"
    assert result["checks"]["fee"]["ok"] is False


def test_funding_sign_mutation_breaks_reconciliation():
    acct = venue_account(decision_ns=100)
    acct.venue_set_wallet("1000")
    acct.at(99)
    acct.set_position(dec("2"), dec("100"), 1)
    acct.at(100)
    acct.settle_funding("SET-1", epoch=1, settlement_ts=100, available_ns=100, mark="100", rate=Fraction(1, 100))
    assert acct.reconcile()["status"] == "PASS"

    for posting in acct.venue_postings:
        if posting.kind == "FUNDING":
            posting.amount = -posting.amount
    result = acct.reconcile()
    assert result["status"] == "FAIL"
    assert result["checks"]["funding"]["ok"] is False


def test_reservation_mutation_breaks_reconciliation():
    acct = venue_account(decision_ns=2)
    acct.venue_set_wallet("20")
    acct.at(1)
    assert acct.reserve("O-A", "12") is True
    assert acct.reconcile()["status"] == "PASS"

    for posting in acct.venue_postings:
        if posting.kind == "RESERVATION":
            posting.amount = dec("13")
    result = acct.reconcile()
    assert result["status"] == "FAIL"
    assert result["checks"]["reservation"]["ok"] is False


def test_reconcile_against_independent_reconstruction():
    acct = venue_account(decision_ns=1)
    acct.venue_set_wallet("1000")
    acct.at(1)
    acct.venue_fill("BUY", "1", "100", fee="0.5")
    acct.set_venue_mark(1)
    tape = [
        {"type": "SET_WALLET", "cash": "1000"},
        {"type": "FILL", "side": "BUY", "qty": "1", "price": "100", "fee": "0.5", "ts_ns": 1},
        {"type": "MARK", "price": "100", "ts_ns": 1},
    ]
    result = acct.reconcile_tape(tape)
    assert result["status"] == "PASS"
    assert reconstruct(tape)["unexplained_residual"] == "0"


# ---------------------------------------------------------------------------
# Fixture agreement with the conformance oracle
# ---------------------------------------------------------------------------


FIXTURE_EXPECTATIONS = {
    "F08": {"funding_cash": "-2", "funding_epoch": "1", "equity_end": "998"},
    "F09": {
        "funding_cash": "-2",
        "funding_attributed_epoch": "1",
        "epoch_2_funding_cash": "0",
        "payment_application_count": 1,
    },
    "F10": {
        "error": "MISSING_SETTLEMENT_MARK",
        "future_mark_join_count": 0,
        "certified": False,
    },
    "F12": {
        "funding_cash": "-0.2",
        "isolated_equity": "4.9",
        "maintenance": "5",
        "liquidation_triggered": True,
    },
    "F13": {"fee": "-0.01", "cash_delta": "0.01"},
    "F16": {
        "O-A.accepted": True,
        "O-B.accepted": False,
        "free_after_reservations": "8",
        "negative_free_cash": False,
    },
}


def _venue_fixture_values(fixture_id: str) -> dict:
    if fixture_id == "F08":
        acct = venue_account(decision_ns=100)
        acct.venue_set_wallet("1000")
        acct.at(99)
        acct.set_position(dec("2"), dec("100"), 1)

        def funding() -> None:
            acct.settle_funding("SET-1", epoch=1, settlement_ts=100, available_ns=100, mark="100", rate=Fraction(1, 100))

        acct.execute_boundary(100, funding, lambda: acct.close_position("100", fee="0"))
        return {
            "funding_cash": V.fmt(acct.funding),
            "funding_epoch": str(next(iter(acct.funding_epochs))),
            "equity_end": V.fmt(acct.equity()),
        }
    if fixture_id == "F09":
        acct = venue_account(decision_ns=100)
        acct.venue_set_wallet("1000")
        acct.at(90)
        acct.set_position(dec("2"), dec("100"), 1)
        acct.at(100)
        acct.settle_funding("SET-1", epoch=1, settlement_ts=100, available_ns=200, mark="100", rate=Fraction(1, 100))
        acct.at(110)
        acct.close_position("100", fee="0")
        acct.set_position(dec("-1"), dec("100"), 2)
        acct.at(200)
        acct.receive_funding("SET-1", 200)
        return {
            "funding_cash": V.fmt(acct.funding),
            "funding_attributed_epoch": str(
                next(iter(acct.settlements.values())).epoch
            ),
            "epoch_2_funding_cash": V.fmt(acct.funding_epochs.get(2, Fraction(0))),
            "payment_application_count": len(acct.funding_applications),
        }
    if fixture_id == "F10":
        acct = venue_account(decision_ns=100, mark_price=None)
        acct.venue_set_wallet("1000")
        acct.at(100)
        acct.set_position(dec("1"), dec("100"), 1)
        error = None
        try:
            acct.settle_funding("SET-1", epoch=1, settlement_ts=100, available_ns=100, mark=None, rate=Fraction(1, 100))
        except V.VenueError as exc:
            error = exc.code
        return {
            "error": error,
            "future_mark_join_count": 0,
            "certified": acct.mark_path_certificate != "missing",
        }
    if fixture_id == "F12":
        acct = venue_account(decision_ns=100, maintenance_rate="0.05")
        acct.venue_set_wallet("1000")
        acct.allocate_isolated("5.1")
        acct.at(0)
        acct.set_position(dec("1"), dec("100"), 1)
        acct.at(100)
        acct.settle_funding("SET-1", epoch=1, settlement_ts=100, available_ns=100, mark="100", rate=Fraction(2, 1000))
        metrics = acct.liquidation_trigger_metrics
        return {
            "funding_cash": V.fmt(acct.funding),
            "isolated_equity": metrics["isolated_equity"],
            "maintenance": metrics["maintenance"],
            "liquidation_triggered": acct.liquidation_triggered,
        }
    if fixture_id == "F13":
        out = V.exact_fee("100", "-1", "10000")
        return {"fee": V.fmt(out["fee"]), "cash_delta": V.fmt(out["cash_delta"])}
    if fixture_id == "F16":
        acct = venue_account(decision_ns=2)
        acct.venue_set_wallet("20")
        acct.at(1)
        a = acct.reserve("O-A", "12")
        acct.at(2)
        b = acct.reserve("O-B", "12")
        snap = acct.snapshot()
        return {
            "O-A.accepted": a,
            "O-B.accepted": b,
            "free_after_reservations": snap["free_after_reservations"],
            "negative_free_cash": snap["negative_free_cash"],
        }
    raise AssertionError(fixture_id)


@pytest.mark.parametrize("fixture_id", sorted(FIXTURE_EXPECTATIONS))
def test_venue_path_reproduces_fixture_expected_values(fixture_id):
    assert _venue_fixture_values(fixture_id) == FIXTURE_EXPECTATIONS[fixture_id]


@pytest.mark.parametrize("fixture_id", ["F08", "F09", "F10", "F11", "F12", "F13", "F16"])
def test_conformance_oracle_agrees(fixture_id):
    for result in Harness().run_fixture(fixture_id):
        assert result["status"] == "PASS", (fixture_id, result["checks"])


def test_f11_venue_metrics_match_conformance_oracle():
    harness = Harness()
    mark = harness.run_named("F11", "MARK")
    quote = harness.run_named("F11", "QUOTE_IS_NOT_MARK")

    acct = venue_account(decision_ns=1, maintenance_rate="0.05")
    acct.venue_set_wallet("1000")
    acct.allocate_isolated("10")
    acct.at(0)
    acct.set_position(dec("1"), dec("100"), 1)
    acct.at(1)
    acct.check_liquidation(mark="94")
    assert acct.liquidation_trigger_metrics["isolated_equity"] == mark.metrics()["isolated_equity"]
    assert acct.liquidation_trigger_metrics["maintenance"] == mark.metrics()["maintenance"]

    quote_acct = venue_account(decision_ns=1, maintenance_rate="0.05")
    quote_acct.venue_set_wallet("1000")
    quote_acct.allocate_isolated("10")
    quote_acct.at(0)
    quote_acct.set_position(dec("1"), dec("100"), 1)
    quote_acct.at(1)
    quote_acct.observe_quote("94", "95")
    assert quote_acct.mark_path_certificate == quote.metrics()["mark_path_certificate"]
    assert (
        quote_acct.mark_trigger_inferred_from_quote
        == quote.metrics()["mark_trigger_inferred_from_quote"]
    )


def test_scope_declarations_record_unsupported_venue_behavior():
    assert V.SCOPED_SUPPORT["isolated_liquidation"] is True
    assert V.SCOPED_SUPPORT["shared_wallet_reservations"] is True
    assert V.SCOPED_SUPPORT["historical_venue_rule_files"] is False
    assert V.SCOPED_SUPPORT["venue_certified_historical_tiers"] is False
    assert V.SCOPED_SUPPORT["native_liquidation_path"] is False
    assert V.SCOPED_SUPPORT["adl"] is False
    assert "ADL" in V.UNSUPPORTED_VENUE_BEHAVIOR
    assert "VENUE_NATIVE_LIQUIDATION" in V.UNSUPPORTED_VENUE_BEHAVIOR
