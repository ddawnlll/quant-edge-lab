"""Point-in-time venue profile: rules, funding, margin and liquidation.

This module binds a venue rule book to the independent exact ``Account``
oracle in :mod:`qel.accounting.core`. It intentionally composes with the
existing account (no hidden cash moves) and adds only what TASK-10 needs:

* bitemporal venue rules with ``valid_from``/``valid_to`` and ``known_at``
  gating, fail-closed when a rule is missing, not yet known at the decision
  time, or contradicts another rule at the same valid+known instant;
* funding settlement with an attributable position epoch, exact currency
  amount and event identity, applied at most once, with late receipts that
  are attributed to the epoch that earned them;
* isolated margin tiers, maintenance-margin liquidation at the declared mark
  (never a quote), forced close with an exact liquidation cost, and explicit
  ``liquidation_triggered``;
* settlement-boundary ordering (funding before a same-time close);
* exact reconciliation between venue postings and the independent account.

Scope boundary (recorded, not invented): no historical venue rule files are
available, so this module does not claim venue-certified historical tiers.
The pinned native engine has no liquidation path, so native liquidation is
not claimed either. See ``evidence/labd/VENUE_LIMITATIONS.json``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable

from .core import Account, AccountingError, dec, fmt, money

# Explicit, stable error codes used by tests and evidence.
UNKNOWN_RULE = "UNKNOWN_RULE"
LOOKAHEAD_RULE = "LOOKAHEAD_RULE"
CONFLICTING_RULE = "CONFLICTING_RULE"
INVALID_RULE = "INVALID_RULE"
MISSING_SETTLEMENT_MARK = "MISSING_SETTLEMENT_MARK"
FUTURE_ONLY_MARK = "FUTURE_ONLY_MARK"
UNKNOWN_MARK_SOURCE = "UNKNOWN_MARK_SOURCE"
TIER_NOT_FOUND = "TIER_NOT_FOUND"
LEVERAGE_EXCEEDED = "LEVERAGE_EXCEEDED"
TRADING_HALTED = "TRADING_HALTED"
DUPLICATE_SETTLEMENT = "DUPLICATE_SETTLEMENT"
UNKNOWN_SETTLEMENT = "UNKNOWN_SETTLEMENT"
FUNDING_NOT_AVAILABLE = "FUNDING_NOT_AVAILABLE"
NUMERIC_OVERFLOW = "NUMERIC_OVERFLOW"
RECONCILIATION_FAILURE = "RECONCILIATION_FAILURE"

RULE_KINDS = frozenset(
    {
        "FEE",
        "FUNDING",
        "MARGIN_TIER",
        "MARK",
        "TRADING_STATUS",
        "SETTLEMENT_ORDER",
    }
)

SETTLEMENT_ORDER_BEFORE = "BEFORE_SAME_TIME_COMMANDS"
SETTLEMENT_ORDER_AFTER = "AFTER_SAME_TIME_COMMANDS"

# What this Python profile does and does not implement. The unsupported
# entries are recorded rather than invented; see VENUE_LIMITATIONS.json.
SCOPED_SUPPORT = {
    "isolated_liquidation": True,
    "shared_wallet_reservations": True,
    "funding_epoch_attribution": True,
    "settlement_boundary_ordering": True,
    "historical_venue_rule_files": False,
    "venue_certified_historical_tiers": False,
    "native_liquidation_path": False,
    "adl": False,
    "auto_deleveraging": False,
    "partial_liquidation": False,
    "cross_margin_liquidation": False,
}

UNSUPPORTED_VENUE_BEHAVIOR = (
    "ADL",
    "VENUE_NATIVE_LIQUIDATION",
    "HISTORICAL_VENUE_RULE_FILES",
    "CROSS_MARGIN_LIQUIDATION",
    "PARTIAL_LIQUIDATION",
)


class VenueError(AccountingError):
    """A fail-closed venue rule/risk violation."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class VenueRule:
    """A bitemporal venue rule.

    ``valid_from_ns``/``valid_to_ns`` are the economic interval (half-open).
    ``known_from_ns``/``known_to_ns`` are the epistemic interval: a rule is
    only usable when the decision time falls inside it. ``source`` is
    provenance; the default is a test fixture, never a historical venue file.
    """

    kind: str
    value: Any
    valid_from_ns: int
    valid_to_ns: int
    known_from_ns: int
    known_to_ns: int | None = None
    source: str = "TEST_FIXTURE"

    def canonical_value(self) -> str:
        return _canonical(self.value)

    def covers(self, at_ns: int) -> bool:
        return self.valid_from_ns <= at_ns < self.valid_to_ns

    def known_at(self, decision_ns: int) -> bool:
        if decision_ns < self.known_from_ns:
            return False
        if self.known_to_ns is not None and decision_ns >= self.known_to_ns:
            return False
        return True


def rule(
    kind: str,
    value: Any,
    *,
    valid_from: int,
    valid_to: int,
    known_at: int,
    known_to: int | None = None,
    source: str = "TEST_FIXTURE",
) -> VenueRule:
    """Convenience constructor; ``known_at`` is the decision-time gate."""

    return VenueRule(
        kind=kind,
        value=value,
        valid_from_ns=int(valid_from),
        valid_to_ns=int(valid_to),
        known_from_ns=int(known_at),
        known_to_ns=None if known_to is None else int(known_to),
        source=source,
    )


class VenueRuleBook:
    """Ordered bitemporal rule set with fail-closed resolution."""

    def __init__(self, rules: list[VenueRule] | None = None):
        self._rules: dict[str, list[VenueRule]] = {kind: [] for kind in RULE_KINDS}
        for item in rules or []:
            self.add(item)

    def __len__(self) -> int:
        return sum(len(items) for items in self._rules.values())

    def add(self, item: VenueRule) -> VenueRule:
        if item.kind not in RULE_KINDS:
            raise VenueError(UNKNOWN_RULE, f"unsupported rule kind {item.kind}")
        if item.valid_from_ns >= item.valid_to_ns:
            raise VenueError(INVALID_RULE, f"{item.kind} empty valid interval")
        if item.known_to_ns is not None and item.known_from_ns > item.known_to_ns:
            raise VenueError(INVALID_RULE, f"{item.kind} empty known interval")
        for existing in self._rules[item.kind]:
            same_window = (
                existing.valid_from_ns == item.valid_from_ns
                and existing.valid_to_ns == item.valid_to_ns
                and existing.known_from_ns == item.known_from_ns
                and existing.known_to_ns == item.known_to_ns
            )
            if same_window and existing.canonical_value() != item.canonical_value():
                raise VenueError(
                    CONFLICTING_RULE,
                    f"{item.kind} conflicts at valid=[{item.valid_from_ns},{item.valid_to_ns}) "
                    f"known={item.known_from_ns}",
                )
        self._rules[item.kind].append(item)
        return item

    def for_kind(self, kind: str) -> list[VenueRule]:
        return list(self._rules.get(kind, []))

    def has_future(self, kind: str, at_ns: int) -> bool:
        return any(item.valid_from_ns > at_ns for item in self._rules.get(kind, []))

    def resolve(self, kind: str, at_ns: int, decision_ns: int) -> VenueRule:
        rules = self._rules.get(kind, [])
        valid = [r for r in rules if r.covers(at_ns)]
        if not valid:
            raise VenueError(UNKNOWN_RULE, f"no {kind} rule valid at {at_ns}")
        eligible = [r for r in valid if r.known_at(decision_ns)]
        if not eligible:
            raise VenueError(
                LOOKAHEAD_RULE,
                f"{kind} rule valid at {at_ns} is not known by decision {decision_ns}",
            )
        latest_known = max(r.known_from_ns for r in eligible)
        latest = [r for r in eligible if r.known_from_ns == latest_known]
        if len({r.canonical_value() for r in latest}) > 1:
            raise VenueError(CONFLICTING_RULE, f"{kind} has conflicting rules at {latest_known}")
        return latest[-1]


# ---------------------------------------------------------------------------
# Arithmetic helpers (F13 precision / rebate / overflow)
# ---------------------------------------------------------------------------


def exact_fee(notional: str | int | Fraction, rate_num: str | int, rate_den: str | int, exponent: int = 2) -> dict:
    """Signed exact fee. A negative rate is a rebate and yields a negative fee."""

    raw = dec(notional) * dec(rate_num) / dec(rate_den)
    fee = money(raw, exponent)
    return {"fee": fee, "cash_delta": -fee}


def checked_multiply(values: list[str | int], bits: int = 128) -> int:
    """Checked signed multiplication retaining the declared integer width."""

    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    acc = 1
    for value in values:
        x = int(value)
        if not lo <= x <= hi:
            raise VenueError(NUMERIC_OVERFLOW, f"operand {x} outside int{bits}")
        product = acc * x
        if not lo <= product <= hi:
            raise VenueError(NUMERIC_OVERFLOW, f"product outside int{bits}")
        acc = product
    return acc


def round_qty(requested: str | int | Fraction, lot: str | int | Fraction) -> Fraction:
    """Lot-floor quantity rounding (never rounds up)."""

    lot_f = dec(lot)
    if lot_f <= 0:
        raise VenueError(INVALID_RULE, "lot must be positive")
    return math.floor(dec(requested) / lot_f) * lot_f


# ---------------------------------------------------------------------------
# Venue profile: a decision-time view over the rule book
# ---------------------------------------------------------------------------


class VenueProfile:
    """A point-in-time venue view bound to one decision time.

    ``default_maintenance_rate`` and ``default_liquidation_fee_rate`` are
    explicit fixture configuration, not historical venue data. When a
    ``MARGIN_TIER`` rule exists it takes precedence.
    """

    def __init__(
        self,
        rules: VenueRuleBook | list[VenueRule] | None = None,
        *,
        decision_ns: int = 0,
        mark_source: str = "EXCHANGE_MARK_PRICE",
        default_maintenance_rate: str | Fraction | None = None,
        default_liquidation_fee_rate: str | Fraction = "0",
        default_max_leverage: str | Fraction | None = None,
        default_settlement_order: str | None = None,
    ):
        self.rules = rules if isinstance(rules, VenueRuleBook) else VenueRuleBook(rules)
        self.decision_ns = int(decision_ns)
        self.mark_source = mark_source
        self.default_maintenance_rate = (
            None if default_maintenance_rate is None else dec(default_maintenance_rate)
        )
        self.default_liquidation_fee_rate = dec(default_liquidation_fee_rate)
        self.default_max_leverage = (
            None if default_max_leverage is None else dec(default_max_leverage)
        )
        self.default_settlement_order = default_settlement_order

    # -- generic ------------------------------------------------------------
    def resolve(self, kind: str, at_ns: int) -> VenueRule:
        return self.rules.resolve(kind, at_ns, self.decision_ns)

    # -- fees ---------------------------------------------------------------
    def fee_rate(self, at_ns: int, liquidity: str = "TAKER") -> Fraction:
        value = self.resolve("FEE", at_ns).value
        key = "taker_rate" if liquidity.upper() == "TAKER" else "maker_rate"
        if key not in value:
            raise VenueError(UNKNOWN_RULE, f"fee schedule missing {key}")
        return dec(value[key])

    def fee(self, notional: str | Fraction, at_ns: int, liquidity: str = "TAKER", exponent: int = 2) -> Fraction:
        return money(dec(notional) * self.fee_rate(at_ns, liquidity), exponent)

    # -- funding ------------------------------------------------------------
    def funding_terms(self, at_ns: int) -> dict:
        value = self.resolve("FUNDING", at_ns).value
        if "rate" not in value:
            raise VenueError(UNKNOWN_RULE, "funding rule missing rate")
        terms = {"rate": dec(value["rate"])}
        if "interval_ns" in value:
            terms["interval_ns"] = int(value["interval_ns"])
        return terms

    # -- marks --------------------------------------------------------------
    def mark(self, at_ns: int) -> tuple[Fraction, str]:
        """Declared mark only. A quote/book is never a mark."""

        try:
            item = self.resolve("MARK", at_ns)
        except VenueError as exc:
            if exc.code == UNKNOWN_RULE:
                if self.rules.has_future("MARK", at_ns):
                    raise VenueError(
                        FUTURE_ONLY_MARK, f"only future marks known at {at_ns}"
                    ) from exc
                raise VenueError(
                    MISSING_SETTLEMENT_MARK, f"no mark for settlement at {at_ns}"
                ) from exc
            raise
        value = item.value
        source = value.get("source", self.mark_source) if isinstance(value, dict) else self.mark_source
        if source != self.mark_source:
            raise VenueError(
                UNKNOWN_MARK_SOURCE,
                f"mark source {source} is not the declared {self.mark_source}",
            )
        price = value.get("price") if isinstance(value, dict) else value
        if price is None:
            raise VenueError(MISSING_SETTLEMENT_MARK, f"mark rule at {at_ns} has no price")
        return dec(price), source

    # -- trading status -----------------------------------------------------
    def trading_status(self, at_ns: int) -> str:
        value = self.resolve("TRADING_STATUS", at_ns).value
        status = value.get("status") if isinstance(value, dict) else value
        if status is None:
            raise VenueError(UNKNOWN_RULE, "trading status rule has no status")
        return str(status)

    def require_trading(self, at_ns: int) -> None:
        status = self.trading_status(at_ns)
        if status != "TRADING":
            raise VenueError(TRADING_HALTED, f"venue status is {status} at {at_ns}")

    # -- settlement ordering ------------------------------------------------
    def settlement_order(self, at_ns: int) -> str:
        try:
            value = self.resolve("SETTLEMENT_ORDER", at_ns).value
        except VenueError as exc:
            if exc.code == UNKNOWN_RULE and self.default_settlement_order is not None:
                return self.default_settlement_order
            raise
        order = value.get("order") if isinstance(value, dict) else value
        if order not in (SETTLEMENT_ORDER_BEFORE, SETTLEMENT_ORDER_AFTER):
            raise VenueError(UNKNOWN_RULE, f"unknown settlement order {order}")
        return str(order)

    # -- margin tiers -------------------------------------------------------
    def margin_tier(self, at_ns: int, notional: str | Fraction) -> dict:
        rule_value = self.resolve("MARGIN_TIER", at_ns).value
        tiers = rule_value.get("tiers") if isinstance(rule_value, dict) else rule_value
        if not tiers:
            raise VenueError(TIER_NOT_FOUND, "margin tier rule has no tiers")
        target = abs(dec(notional))
        ordered = sorted(tiers, key=lambda tier: dec(tier["max_notional"]))
        for tier in ordered:
            if target <= dec(tier["max_notional"]):
                return tier
        raise VenueError(TIER_NOT_FOUND, f"no tier covers notional {fmt(target)}")

    def maintenance_rate(self, at_ns: int, notional: str | Fraction) -> Fraction:
        try:
            tier = self.margin_tier(at_ns, notional)
        except VenueError as exc:
            if exc.code == UNKNOWN_RULE and self.default_maintenance_rate is not None:
                return self.default_maintenance_rate
            raise
        if "maintenance_rate" not in tier:
            raise VenueError(TIER_NOT_FOUND, "tier missing maintenance_rate")
        return dec(tier["maintenance_rate"])

    def liquidation_fee_rate(self, at_ns: int, notional: str | Fraction) -> Fraction:
        try:
            tier = self.margin_tier(at_ns, notional)
        except VenueError as exc:
            if exc.code == UNKNOWN_RULE:
                return self.default_liquidation_fee_rate
            raise
        return dec(tier.get("liquidation_fee_rate", self.default_liquidation_fee_rate))

    def max_leverage(self, at_ns: int, notional: str | Fraction) -> Fraction:
        tier = self.margin_tier(at_ns, notional)
        if "max_leverage" in tier:
            return dec(tier["max_leverage"])
        if self.default_max_leverage is not None:
            return self.default_max_leverage
        raise VenueError(TIER_NOT_FOUND, "tier missing max_leverage")

    def check_leverage(
        self, at_ns: int, notional: str | Fraction, equity: str | Fraction
    ) -> Fraction:
        required = abs(dec(notional))
        cap = abs(dec(equity)) * self.max_leverage(at_ns, notional)
        if required > cap:
            raise VenueError(
                LEVERAGE_EXCEEDED,
                f"notional {fmt(required)} exceeds leverage cap {fmt(cap)}",
            )
        return cap


# ---------------------------------------------------------------------------
# Venue postings and reconciliation
# ---------------------------------------------------------------------------


@dataclass
class VenuePosting:
    kind: str
    amount: Fraction
    at_ns: int
    ref: str = ""
    accepted: bool = True

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "amount": fmt(self.amount),
            "at_ns": self.at_ns,
            "ref": self.ref,
            "accepted": self.accepted,
        }


@dataclass
class FundingSettlement:
    settlement_id: str
    epoch: int
    settlement_ts: int
    available_ns: int
    qty: Fraction
    mark: Fraction
    rate: Fraction
    amount: Fraction
    applied: bool = False
    received_ns: int | None = None

    def as_dict(self) -> dict:
        return {
            "settlement_id": self.settlement_id,
            "epoch": self.epoch,
            "settlement_ts": self.settlement_ts,
            "available_ns": self.available_ns,
            "qty": fmt(self.qty),
            "mark": fmt(self.mark),
            "rate": fmt(self.rate),
            "amount": fmt(self.amount),
            "applied": self.applied,
            "received_ns": self.received_ns,
        }


# ---------------------------------------------------------------------------
# Venue account bound to the independent exact account
# ---------------------------------------------------------------------------


class VenueAccount(Account):
    """An exact ``Account`` extended with a venue profile and risk path.

    The account itself remains the independent oracle for cash, positions and
    postings. Venue postings are a separate ledger used for exact
    reconciliation, so a fee/funding sign or reservation mutation is visible.
    """

    def __init__(self, profile: VenueProfile, currency: str = "USDT"):
        super().__init__(currency=currency)
        self.profile = profile
        self.now_ns = 0
        self.venue_postings: list[VenuePosting] = []
        self.settlements: dict[str, FundingSettlement] = {}
        self.funding_applications: list[str] = []
        self.mark_history: list[tuple[int, Fraction, str]] = []
        self.liquidation_triggered = False
        self.liquidation_events: list[dict] = []
        self.liquidation_trigger_metrics: dict | None = None
        self.mark_path_certificate = "missing"
        self.mark_trigger_inferred_from_quote = False
        self._margin_enabled = (
            profile.default_maintenance_rate is not None
            or bool(profile.rules.for_kind("MARGIN_TIER"))
        )

    # -- clock --------------------------------------------------------------
    def at(self, at_ns: int) -> "VenueAccount":
        if at_ns < self.now_ns:
            raise VenueError("OUT_OF_ORDER", f"{at_ns} precedes {self.now_ns}")
        self.now_ns = int(at_ns)
        return self

    def _record(self, kind: str, amount: Fraction, ref: str = "", accepted: bool = True) -> None:
        self.venue_postings.append(
            VenuePosting(kind=kind, amount=amount, at_ns=self.now_ns, ref=ref, accepted=accepted)
        )

    # -- wallet / reservations ---------------------------------------------
    def venue_set_wallet(self, cash: str | Fraction) -> None:
        self.set_wallet(dec(cash))
        self._record("WALLET", dec(cash), "SET_WALLET")

    def allocate_isolated(self, amount: str | Fraction) -> None:
        amount_f = dec(amount)
        super().allocate_isolated(amount_f)
        self._record("ALLOCATION", amount_f, "ALLOCATE_ISOLATED")

    def reserve(self, reservation_id: str, margin: str | Fraction) -> bool:
        accepted = super().reserve(reservation_id, dec(margin))
        self._record(
            "RESERVATION", dec(margin) if accepted else Fraction(0), reservation_id, accepted
        )
        return accepted

    # -- trading status -----------------------------------------------------
    def require_trading(self) -> None:
        self.profile.require_trading(self.now_ns)

    # -- fees / fills -------------------------------------------------------
    def venue_fill(
        self,
        side: str,
        qty: str | Fraction,
        price: str | Fraction,
        *,
        liquidity: str = "TAKER",
        fee: str | Fraction | None = None,
        check_margin: bool = True,
    ) -> Fraction:
        self.require_trading()
        qty_f, price_f = dec(qty), dec(price)
        if fee is None:
            fee_f = self.profile.fee(qty_f * price_f, self.now_ns, liquidity)
        else:
            fee_f = dec(fee)
        self.fill(side, qty_f, price_f, fee_f)
        self._record("FEE", -fee_f, f"FILL:{side}:{fmt(qty_f)}@{fmt(price_f)}")
        if check_margin and self._margin_enabled and self.position_qty != 0:
            self.check_liquidation()
        return fee_f

    def close_position(
        self, price: str | Fraction, *, liquidity: str = "TAKER", fee: str | Fraction | None = None
    ) -> Fraction | None:
        if self.position_qty == 0:
            return None
        side = "SELL" if self.position_qty > 0 else "BUY"
        return self.venue_fill(side, abs(self.position_qty), price, liquidity=liquidity, fee=fee)

    # -- marks --------------------------------------------------------------
    def set_venue_mark(self, at_ns: int | None = None) -> Fraction:
        at = self.now_ns if at_ns is None else at_ns
        price, source = self.profile.mark(at)
        self.set_mark(price)
        self.mark_history.append((at, price, source))
        self.mark_path_certificate = "observed"
        return price

    def observe_quote(self, bid: str | Fraction, ask: str | Fraction) -> None:
        """Record a quote without ever treating it as a mark.

        F11 ``QUOTE_IS_NOT_MARK``: a book update is not a liquidation trigger
        source. This method deliberately does not touch ``self.mark``.
        """

        self._record("QUOTE", Fraction(0), f"BID:{fmt(dec(bid))}/ASK:{fmt(dec(ask))}")

    def check_liquidation(
        self, mark: str | Fraction | None = None, *, force_close: bool = True
    ) -> bool:
        """Isolated liquidation at the declared mark. Quotes never trigger it."""

        if not self._margin_enabled or self.position_qty == 0:
            return False
        if mark is None:
            price, source = self.profile.mark(self.now_ns)
            self.set_mark(price)
            self.mark_history.append((self.now_ns, price, source))
            self.mark_path_certificate = "observed"
        else:
            price = dec(mark)
            self.set_mark(price)
        maintenance_rate = self.profile.maintenance_rate(self.now_ns, abs(self.position_qty * price))
        maintenance = abs(self.position_qty * price) * maintenance_rate
        equity = self.isolated + self.unrealized()
        if equity <= maintenance:
            self.liquidation_triggered = True
            self.liquidation_trigger_metrics = {
                "at_ns": self.now_ns,
                "mark": fmt(price),
                "isolated_equity": fmt(equity),
                "maintenance": fmt(maintenance),
            }
            if force_close:
                self.force_close(price)
            return True
        return False

    def force_close(self, price: str | Fraction, liquidation_cost: str | Fraction | None = None) -> dict:
        """Forced close with exact realized PnL and explicit liquidation cost."""

        price_f = dec(price)
        qty = self.position_qty
        if qty == 0:
            return {"closed_qty": Fraction(0), "realized": Fraction(0), "cost": Fraction(0)}
        realized = qty * (price_f - self.entry)
        notional = abs(qty * price_f)
        if liquidation_cost is None:
            cost = money(notional * self.profile.liquidation_fee_rate(self.now_ns, notional), 2)
        else:
            cost = dec(liquidation_cost)
        self.realized_physical += realized
        self.isolated += realized
        self.isolated -= cost
        self.liquidation_cost += cost
        self._post("CASH_ISOLATED", "REALIZED_PRICE_PNL", realized, self.epoch)
        self._post("LIQUIDATION_COST", "CASH_ISOLATED", cost, self.epoch)
        self._record("LIQUIDATION", -cost, f"FORCED_CLOSE:{fmt(qty)}@{fmt(price_f)}")
        close_epoch = self.epoch
        self.position_qty = Fraction(0)
        self.entry = Fraction(0)
        self.epoch += 1
        self.liquidation_triggered = True
        event = {
            "at_ns": self.now_ns,
            "epoch": close_epoch,
            "qty": qty,
            "price": price_f,
            "realized": realized,
            "liquidation_cost": cost,
            "isolated_equity_after": self.isolated,
        }
        if self.liquidation_trigger_metrics is not None:
            event.update(
                {
                    "isolated_equity_at_trigger": dec(
                        self.liquidation_trigger_metrics["isolated_equity"]
                    ),
                    "maintenance_at_trigger": dec(
                        self.liquidation_trigger_metrics["maintenance"]
                    ),
                }
            )
        self.liquidation_events.append(event)
        return {"closed_qty": qty, "realized": realized, "cost": cost}

    # -- funding ------------------------------------------------------------
    def schedule_funding(
        self,
        settlement_id: str,
        *,
        epoch: int,
        settlement_ts: int,
        available_ns: int | None = None,
        mark: str | Fraction | None = None,
        rate: str | Fraction | None = None,
    ) -> FundingSettlement:
        """Record a funding settlement at its economic time without posting yet.

        The position quantity is snapshotted at scheduling time, so a late
        receipt cannot be re-priced against a later position.
        """

        if settlement_id in self.settlements:
            raise VenueError(
                DUPLICATE_SETTLEMENT, f"settlement {settlement_id} already recorded"
            )
        epoch_i = int(epoch)
        if epoch_i > self.epoch:
            raise VenueError(
                "PROTOCOL_VIOLATION",
                f"funding references epoch {epoch_i} with no open position history",
            )
        if epoch_i < 1:
            raise VenueError(INVALID_RULE, f"invalid epoch {epoch_i}")
        if mark is None:
            mark_f, _ = self.profile.mark(int(settlement_ts))
        else:
            mark_f = dec(mark)
        rate_f = self.profile.funding_terms(int(settlement_ts))["rate"] if rate is None else dec(rate)
        qty = self.position_qty
        amount = -qty * mark_f * rate_f
        settlement = FundingSettlement(
            settlement_id=settlement_id,
            epoch=epoch_i,
            settlement_ts=int(settlement_ts),
            available_ns=int(available_ns if available_ns is not None else settlement_ts),
            qty=qty,
            mark=mark_f,
            rate=rate_f,
            amount=amount,
        )
        self.settlements[settlement_id] = settlement
        return settlement

    def receive_funding(self, settlement_id: str, at_ns: int | None = None) -> FundingSettlement:
        """Post a scheduled settlement exactly once, to its earning epoch."""

        settlement = self.settlements.get(settlement_id)
        if settlement is None:
            raise VenueError(UNKNOWN_SETTLEMENT, f"settlement {settlement_id} was never scheduled")
        if settlement.applied:
            raise VenueError(
                DUPLICATE_SETTLEMENT, f"settlement {settlement_id} already applied"
            )
        receive = self.now_ns if at_ns is None else int(at_ns)
        if receive < settlement.available_ns:
            raise VenueError(
                FUNDING_NOT_AVAILABLE,
                f"settlement {settlement_id} available at {settlement.available_ns}",
            )
        if receive < settlement.settlement_ts:
            raise VenueError(
                "OUT_OF_ORDER", f"settlement {settlement_id} received before its settlement time"
            )
        self.funding_payment(settlement.amount, settlement.epoch, settlement_id)
        settlement.applied = True
        settlement.received_ns = receive
        self.funding_applications.append(settlement_id)
        self._record("FUNDING", settlement.amount, settlement_id)
        if self._margin_enabled and self.position_qty != 0:
            self.check_liquidation(mark=settlement.mark)
        return settlement

    def settle_funding(
        self,
        settlement_id: str,
        *,
        epoch: int,
        settlement_ts: int | None = None,
        available_ns: int | None = None,
        mark: str | Fraction | None = None,
        rate: str | Fraction | None = None,
    ) -> FundingSettlement:
        at = self.now_ns if settlement_ts is None else int(settlement_ts)
        settlement = self.schedule_funding(
            settlement_id,
            epoch=epoch,
            settlement_ts=at,
            available_ns=available_ns,
            mark=mark,
            rate=rate,
        )
        if settlement.available_ns <= self.now_ns:
            self.receive_funding(settlement_id, self.now_ns)
        return settlement

    # -- settlement boundary ------------------------------------------------
    def execute_boundary(
        self,
        at_ns: int,
        funding: Callable[[], Any],
        same_time: Callable[[], Any],
    ) -> Any:
        """Apply funding before a same-time close according to the declared order."""

        self.at(at_ns)
        order = self.profile.settlement_order(at_ns)
        if order == SETTLEMENT_ORDER_BEFORE:
            funding()
            return same_time()
        if order == SETTLEMENT_ORDER_AFTER:
            same_time()
            return funding()
        raise VenueError(UNKNOWN_RULE, f"unknown settlement order {order}")

    # -- reconciliation -----------------------------------------------------
    def reconcile(self) -> dict:
        """Exact reconciliation of venue postings to the independent account."""

        ledger_fee = sum(
            (p.amount for p in self.venue_postings if p.kind == "FEE"), Fraction(0)
        )
        ledger_funding = sum(
            (p.amount for p in self.venue_postings if p.kind == "FUNDING"), Fraction(0)
        )
        ledger_reserved = sum(
            (p.amount for p in self.venue_postings if p.kind == "RESERVATION"),
            Fraction(0),
        )
        ledger_liquidation = sum(
            (p.amount for p in self.venue_postings if p.kind == "LIQUIDATION"),
            Fraction(0),
        )
        checks = {
            "fee": {
                "venue": fmt(ledger_fee),
                "account": fmt(-self.fees),
                "ok": ledger_fee == -self.fees,
            },
            "funding": {
                "venue": fmt(ledger_funding),
                "account": fmt(self.funding),
                "ok": ledger_funding == self.funding,
            },
            "reservation": {
                "venue": fmt(ledger_reserved),
                "account": fmt(sum(self.reservations.values(), Fraction(0))),
                "ok": ledger_reserved == sum(self.reservations.values(), Fraction(0)),
            },
            "liquidation": {
                "venue": fmt(ledger_liquidation),
                "account": fmt(-self.liquidation_cost),
                "ok": ledger_liquidation == -self.liquidation_cost,
            },
        }
        ok = all(check["ok"] for check in checks.values())
        return {
            "status": "PASS" if ok else "FAIL",
            "code": None if ok else RECONCILIATION_FAILURE,
            "checks": checks,
            "residual": fmt(
                (ledger_fee + self.fees)
                + (ledger_funding - self.funding)
                + (ledger_reserved - sum(self.reservations.values(), Fraction(0)))
                + (ledger_liquidation + self.liquidation_cost)
            ),
        }

    def reconcile_tape(self, tape: list[dict[str, Any]]) -> dict:
        """Compare this venue account to a fresh independent reconstruction."""

        from .core import reconstruct

        independent = reconstruct(tape, currency=self.currency)
        checks = {
            "equity": {
                "venue": fmt(self.equity()),
                "independent": independent["equity"],
                "ok": fmt(self.equity()) == independent["equity"],
            },
            "fee_total": {
                "venue": fmt(self.fees),
                "independent": independent["fee_total"],
                "ok": fmt(self.fees) == independent["fee_total"],
            },
            "funding_cash": {
                "venue": fmt(self.funding),
                "independent": independent["funding_cash"],
                "ok": fmt(self.funding) == independent["funding_cash"],
            },
            "reserved": {
                "venue": fmt(sum(self.reservations.values(), Fraction(0))),
                "independent": independent["reserved"],
                "ok": fmt(sum(self.reservations.values(), Fraction(0)))
                == independent["reserved"],
            },
        }
        ok = all(check["ok"] for check in checks.values())
        return {
            "status": "PASS" if ok else "FAIL",
            "code": None if ok else RECONCILIATION_FAILURE,
            "checks": checks,
        }

    # -- reporting ----------------------------------------------------------
    def snapshot(self) -> dict:
        out = super().snapshot()
        out.update(
            {
                "liquidation_triggered": self.liquidation_triggered,
                "liquidation_events": [
                    {
                        "at_ns": event["at_ns"],
                        "epoch": event["epoch"],
                        "qty": fmt(event["qty"]),
                        "price": fmt(event["price"]),
                        "realized": fmt(event["realized"]),
                        "liquidation_cost": fmt(event["liquidation_cost"]),
                    }
                    for event in self.liquidation_events
                ],
                "liquidation_trigger_metrics": self.liquidation_trigger_metrics,
                "settlements": {
                    key: value.as_dict() for key, value in sorted(self.settlements.items())
                },
                "funding_application_count": len(self.funding_applications),
                "mark_path_certificate": self.mark_path_certificate,
                "mark_trigger_inferred_from_quote": self.mark_trigger_inferred_from_quote,
                "venue_postings": [p.as_dict() for p in self.venue_postings],
                "reconciliation": self.reconcile(),
            }
        )
        return out
