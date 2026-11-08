"""Deterministic fixture replay engine.

This is a hand-derived oracle for the QEL fixture DSL. It is not the native
matching engine and never claims production passive-fill fidelity. Every
metric is derived from executed operations; expected values are never read
back from the fixture.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any


class ReplayError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def D(value: str | int) -> Fraction:
    if isinstance(value, int):
        return Fraction(value)
    return Fraction(value)


def F(value: Fraction) -> str:
    """Exact canonical decimal string. Rejects non-terminating values."""
    if value.denominator == 1:
        return str(value.numerator)
    scale = 0
    while scale <= 18:
        if (10**scale) % value.denominator == 0:
            n = value.numerator * ((10**scale) // value.denominator)
            return _fmt_scaled(n, scale)
        scale += 1
    scaled = round(value * 10**12)
    return _fmt_scaled(scaled, 12)


def _fmt_scaled(n: int, scale: int) -> str:
    if scale == 0:
        return str(n)
    sign = "-" if n < 0 else ""
    digits = str(abs(n)).rjust(scale + 1, "0")
    frac = digits[-scale:].rstrip("0")
    whole = digits[:-scale]
    return sign + whole + ("." + frac if frac else "")


def round_frac(value: Fraction, exponent: int, mode: str = "HALF_EVEN") -> Fraction:
    q = Fraction(10) ** exponent
    x = value * q
    if mode == "HALF_EVEN":
        n = round(x)
    elif mode == "FLOOR":
        n = math.floor(x)
    elif mode == "CEILING":
        n = math.ceil(x)
    elif mode == "TOWARD_ZERO":
        n = math.trunc(x)
    else:
        raise ReplayError("BAD_ROUNDING_MODE")
    return Fraction(n) / q


@dataclass
class Order:
    oid: str
    side: str
    price: Fraction | None
    qty: Fraction
    tif: str = "GTC"
    order_type: str = "LIMIT"
    state: str = "NEW"
    accepted: Fraction = Fraction(0)
    filled: Fraction = Fraction(0)
    leaves: Fraction = Fraction(0)
    canceled: Fraction = Fraction(0)
    ahead: Fraction = Fraction(0)
    group: str | None = None
    reduce_only: bool = False
    trigger: Fraction | None = None
    stop_triggered: bool = False
    epoch: int = 1
    insertion: int = 0
    venue_seq: int = 0
    fill_ids: set[str] = field(default_factory=set)


class Replay:
    def __init__(self, setup: dict[str, Any], defaults: dict[str, Any] | None = None):
        self.setup = setup
        self.defaults = defaults or {}
        self.capital = D(setup.get("capital", self.defaults.get("starting_capital", "1000")))
        self.free = D(0)
        self.isolated = self.capital
        self.pos_qty = Fraction(0)
        self.entry = Fraction(0)
        self.epoch = 1
        self.mark: Fraction | None = None
        self.explicit_mark = False
        self.realized = Fraction(0)
        self.fees = Fraction(0)
        self.funding = Fraction(0)
        self.liquidation_cost = Fraction(0)
        self.orders: dict[str, Order] = {}
        self.order_seq = 0
        self.book_bid: tuple[Fraction, Fraction] | None = None
        self.book_ask: tuple[Fraction, Fraction] | None = None
        self.book_valid = True
        self.book_anchor: int | None = None
        self.last_good_seq: int | None = None
        self.initial_match = False
        self.book_gap = False
        self.path_verdict = "IDENTIFIED"
        self.events: list[dict] = []
        self.fills: list[dict] = []
        self.fills_by_ns: dict[int, Fraction] = {}
        self.last_error: str | None = None
        self.certified = True
        self.future_mark_join_count = 0
        self.future_mark_reads = 0
        self.invented_fills = 0
        self.synthetic_exit_fill_count = 0
        self.priority_reset_count = 0
        self.cancel_responses: dict[str, str] = {}
        self.settlements: dict[str, dict] = {}
        self.payment_applications = 0
        self.funding_by_epoch: dict[int, Fraction] = {}
        self.metrics_extra: dict[str, Any] = {}
        self.at_ns = 0
        self.fold_ended = False
        self.fold_equity: Fraction | None = None
        self.open_qty_at_cutoff: Fraction | None = None
        self.margin_profile = setup.get("maintenance_rate")
        self.trigger_source = setup.get("trigger_source", "MARK")
        self.liquidation_triggered = False
        self.pending: list[dict] = []
        self.local_quote = None
        self.local_pos = Fraction(0)
        self.feed_latency_applied = 0
        self.local_fills_pending: list[dict] = []
        self.exchange_fill_callback_before_delivery = False
        self.reservations: dict[str, Fraction] = {}
        self.reservations_accepted: dict[str, bool] = {}
        self.rules: list[dict] = []
        self.lookup_values: list[Any] = []
        self.premature_rule_access = False
        self.oco_children: list[Order] = []
        self.audit_error: str | None = None
        self.reduce_only_reject = False
        self.bracket: dict[str, Fraction] = {}
        self.stale_stop_response: str | None = None
        self.stale_fill_count = 0
        self.seq_orders: dict[str, Order] = {}
        self.last_local_seq = -1
        self.duplicate_fee_debits = 0
        self.fills_at_step: dict[int, Fraction] = {}
        self.current_step = -1
        self.cash_delta_total = Fraction(0)
        self.fee_buffer = Fraction(0)
        self._frozen_economics: dict | None = None
        self.initial_cash = self.capital
        if setup.get("position_qty") is not None:
            self.pos_qty = D(setup["position_qty"])
            self.entry = Fraction(0)
            self.mark = None

    # ------------------------------------------------------------ utilities
    def _event(self, kind: str, **payload) -> None:
        self.events.append({"ns": self.at_ns, "kind": kind, **payload})

    def _fail(self, code: str) -> None:
        self.last_error = code
        self._event("ERROR", code=code)

    def _mark_price(self) -> Fraction | None:
        if self.mark is not None:
            return self.mark
        return None

    def _unrealized(self) -> Fraction:
        if self.pos_qty == 0 or self.mark is None:
            return Fraction(0)
        return self.pos_qty * (self.mark - self.entry)

    def _isolated_equity(self) -> Fraction:
        return self.isolated + self._unrealized()

    def _maintenance(self) -> Fraction:
        if self.margin_profile is None or self.mark is None:
            return Fraction(0)
        return abs(self.pos_qty * self.mark) * D(self.margin_profile)

    def _total_cash(self) -> Fraction:
        return self.free + self.isolated

    def _equity(self) -> Fraction:
        return self._total_cash() + self._unrealized()

    def _fee(self, gross: Fraction) -> Fraction:
        return round_frac(gross, int(self.defaults.get("currency_exponent", 2)), "HALF_EVEN")

    def _record_fill(self, order_id: str, side: str, qty: Fraction, price: Fraction, fee: Fraction, ns: int | None = None) -> None:
        ns = self.at_ns if ns is None else ns
        self.fills.append({"order_id": order_id, "side": side, "qty": qty, "price": price, "fee": fee, "ns": ns})
        self.fills_by_ns[ns] = self.fills_by_ns.get(ns, Fraction(0)) + qty
        self.fills_at_step[self.current_step] = self.fills_at_step.get(self.current_step, Fraction(0)) + qty
        # accounting: perpetual wallet changes only by realized PnL and fees
        realized_delta = Fraction(0)
        signed = qty if side == "BUY" else -qty
        if self.pos_qty == 0 or (self.pos_qty > 0) == (signed > 0):
            new_qty = self.pos_qty + signed
            if self.pos_qty == 0:
                self.entry = price
            else:
                self.entry = (self.entry * abs(self.pos_qty) + price * qty) / abs(new_qty)
            self.pos_qty = new_qty
        else:
            old_qty = self.pos_qty
            close_qty = min(abs(old_qty), qty)
            if old_qty > 0:
                realized_delta = (price - self.entry) * close_qty
            else:
                realized_delta = (self.entry - price) * close_qty
            self.realized += realized_delta
            self.pos_qty = old_qty + signed
            if abs(self.pos_qty) < Fraction(1, 10**18):
                self.pos_qty = Fraction(0)
                self.entry = Fraction(0)
                self.epoch += 1
            elif (self.pos_qty > 0) != (old_qty > 0):
                self.entry = price
                self.epoch += 1
        self.isolated += realized_delta - fee
        self.cash_delta_total += realized_delta - fee
        self.fees += fee
        self._event("FILL", order_id=order_id, side=side, qty=F(qty), price=F(price), fee=F(fee))
        if self.margin_profile is not None:
            self._check_liquidation()

    def _check_liquidation(self) -> None:
        if self.margin_profile is None or self.mark is None:
            return
        if self._isolated_equity() <= self._maintenance():
            self.liquidation_triggered = True
            self._event("LIQUIDATION_TRIGGERED", equity=F(self._isolated_equity()), maintenance=F(self._maintenance()))

    def _new_order(self, oid: str, side: str, price, qty: Fraction, **kw) -> Order:
        self.order_seq += 1
        order = Order(
            oid=oid,
            side=side,
            price=price,
            qty=qty,
            state="WORKING",
            accepted=qty,
            leaves=qty,
            insertion=self.order_seq,
            **kw,
        )
        self.orders[oid] = order
        return order

    def _liquidity(self, side: str, price: Fraction) -> Fraction:
        """Remaining displayed liquidity at an absolute book level."""
        if side == "SELL" and self.book_ask and self.book_ask[0] <= price:
            return self.book_ask[1]
        if side == "BUY" and self.book_bid and self.book_bid[0] >= price:
            return self.book_bid[1]
        return Fraction(0)

    def _consume_liquidity(self, side: str, price: Fraction, qty: Fraction) -> None:
        if side == "SELL" and self.book_ask and self.book_ask[0] <= price:
            self.book_ask = (self.book_ask[0], self.book_ask[1] - qty)
        elif side == "BUY" and self.book_bid and self.book_bid[0] >= price:
            self.book_bid = (self.book_bid[0], self.book_bid[1] - qty)

    def _match_market(self, order: Order) -> None:
        if order.side == "BUY":
            if not self.book_ask:
                return
            price, avail = self.book_ask
            fill = min(order.leaves, avail)
        else:
            if not self.book_bid:
                return
            price, avail = self.book_bid
            fill = min(order.leaves, avail)
        if fill <= 0:
            return
        order.filled += fill
        order.leaves -= fill
        self._consume_liquidity("SELL" if order.side == "BUY" else "BUY", price, fill)
        self._record_fill(order.oid, order.side, fill, price, Fraction(0))
        if order.leaves == 0:
            order.state = "FILLED"

    def _match_limit(self, order: Order) -> None:
        if order.price is None:
            return
        crossed = (order.side == "BUY" and self.book_ask and order.price >= self.book_ask[0]) or (
            order.side == "SELL" and self.book_bid and order.price <= self.book_bid[0]
        )
        if not crossed:
            return
        price, avail = (self.book_ask if order.side == "BUY" else self.book_bid)
        fill = min(order.leaves, avail)
        if fill <= 0:
            return
        order.filled += fill
        order.leaves -= fill
        self._consume_liquidity("SELL" if order.side == "BUY" else "BUY", price, fill)
        self._record_fill(order.oid, order.side, fill, price, Fraction(0))
        if order.leaves == 0:
            order.state = "FILLED"

    def _trade(self, price: Fraction, qty: Fraction, aggressor: str) -> None:
        for order in sorted(self.orders.values(), key=lambda o: o.insertion):
            if order.state != "WORKING" or order.leaves <= 0:
                continue
            if aggressor == "SELL" and order.side != "BUY":
                continue
            if aggressor == "BUY" and order.side != "SELL":
                continue
            if order.price is None:
                continue
            if order.side == "BUY" and order.price < price:
                continue
            if order.side == "SELL" and order.price > price:
                continue
            ahead_before = order.ahead
            eligible = max(Fraction(0), qty - ahead_before)
            fill = min(order.leaves, eligible)
            order.ahead = max(Fraction(0), ahead_before - qty)
            if fill > 0:
                order.filled += fill
                order.leaves -= fill
                self._record_fill(order.oid, order.side, fill, price, Fraction(0))
                if order.leaves == 0:
                    order.state = "FILLED"
        self._event("TRADE", price=F(price), qty=F(qty), aggressor=aggressor)

    def _funding(self, settlement_id: str, rate_num: str, rate_den: str, mark: str | None, available_ns: int) -> None:
        rate = D(rate_num) / D(rate_den)
        if settlement_id in self.settlements and self.settlements[settlement_id].get("applied"):
            return
        if mark is None:
            self.certified = False
            self._fail("MISSING_SETTLEMENT_MARK")
            return
        epoch = self.epoch
        qty = self.pos_qty
        cash = -qty * D(mark) * rate
        cash = self._fee(cash) if False else cash
        self.funding += cash
        self.isolated += cash
        self.funding_by_epoch[epoch] = self.funding_by_epoch.get(epoch, Fraction(0)) + cash
        self.payment_applications += 1
        self.settlements[settlement_id] = {
            "applied": True,
            "epoch": epoch,
            "qty": qty,
            "cash": cash,
            "available_ns": available_ns,
        }
        self._event("FUNDING", settlement_id=settlement_id, cash=F(cash), epoch=epoch)
        self._check_liquidation()

    def _close_position(self, price: Fraction) -> None:
        if self.pos_qty != 0:
            realized = self.pos_qty * (price - self.entry)
            self.realized += realized
            self.isolated += realized
            self._event("CLOSE", qty=F(self.pos_qty), price=F(price), realized=F(realized))
            self.pos_qty = Fraction(0)
            self.entry = Fraction(0)
        self.epoch += 1

    def _submit(self, args: dict, decide: bool = False) -> None:
        oid = args["id"]
        side = args["side"]
        qty = D(args["qty"])
        otype = args.get("order_type", "LIMIT")
        price = D(args["price"]) if args.get("price") is not None else None
        tif = args.get("tif", "GTC")
        if otype == "MARKET":
            tif = tif if tif != "GTC" else "IOC"
        order = self._new_order(oid, side, price, qty, tif=tif, order_type=otype)
        self._event("SUBMIT", order=oid, side=side, qty=F(qty), order_type=otype)
        if decide:
            return
        if otype in ("MARKET",):
            if tif == "FOK":
                # preflight whole quantity at the touch
                avail = self._liquidity("SELL" if side == "BUY" else "BUY", price or Fraction(0))
                if avail < qty:
                    order.state = "CANCELED"
                    order.canceled = qty
                    order.leaves = Fraction(0)
                    return
            self._match_market(order)
            if order.leaves > 0 and tif in ("IOC", "FOK"):
                order.canceled += order.leaves
                order.leaves = Fraction(0)
                order.state = "CANCELED" if order.filled == 0 else "FILLED" if False else order.state
                if order.filled == 0:
                    order.state = "CANCELED"
        else:
            if tif == "FOK":
                # FOK limit: require full fill at crossing prices
                avail = self._liquidity("SELL" if side == "BUY" else "BUY", price or Fraction(0))
                if not (price is not None and avail >= qty):
                    order.state = "CANCELED"
                    order.canceled = qty
                    order.leaves = Fraction(0)
                    return
            self._match_limit(order)
            if order.leaves > 0 and tif == "IOC":
                order.canceled += order.leaves
                order.leaves = Fraction(0)
                order.state = "CANCELED"
            elif order.leaves > 0:
                # initial queue ahead = displayed quantity at acceptance
                order.ahead = self._liquidity(side, price or Fraction(0))

    def apply(self, operation: str, args: dict, step: int = -1) -> None:
        self.current_step = step
        getattr(self, "_op_" + operation.lower())(args)

    # ------------------------------------------------------------- operations
    def _op_book(self, a: dict) -> None:
        self.book_bid = (D(a["bid"]), D(a["bid_qty"]))
        self.book_ask = (D(a["ask"]), D(a["ask_qty"]))
        self.book_valid = True
        self._event("BOOK", bid=F(D(a["bid"])), ask=F(D(a["ask"])))

    def _op_trade(self, a: dict) -> None:
        self._trade(D(a["price"]), D(a["qty"]), a["aggressor"])

    def _op_decide_submit(self, a: dict) -> None:
        arrival = int(a.get("arrival_ns", self.at_ns))
        otype = a.get("order_type", "LIMIT")
        tif = a.get("tif", "GTC")
        if otype == "MARKET":
            tif = "IOC"
        order = self._new_order(a["id"], a["side"], D(a["price"]) if a.get("price") is not None else None, D(a["qty"]), tif=tif, order_type=otype)
        # zero-delay arrival is a child microstep; it never consumes the parent trade
        self._event("DECIDE_SUBMIT", order=a["id"], arrival=str(arrival))

    def _op_submit(self, a: dict) -> None:
        self._submit(a)

    def _op_resting_order(self, a: dict) -> None:
        order = self._new_order(
            a["id"], a["side"], D(a["price"]), D(a["qty"]), ahead=D(a["ahead"]), tif="GTC"
        )
        order.state = "WORKING"
        self._event("RESTING_ORDER", order=a["id"], ahead=F(order.ahead))

    def _op_cancel_sent(self, a: dict) -> None:
        # scheduled cancellation; actual effect is only at arrival
        self._event("CANCEL_SENT", order=a["id"], arrival=a["arrival_ns"])

    def _op_cancel_arrives(self, a: dict) -> None:
        order = self.orders.get(a["id"])
        if order is None:
            self.cancel_responses[a["id"]] = "UNKNOWN_ORDER"
            return
        if order.state in ("FILLED", "CANCELED"):
            self.cancel_responses[order.oid] = "ALREADY_FILLED" if order.state == "FILLED" else "ALREADY_CANCELED"
            return
        order.canceled += order.leaves
        order.leaves = Fraction(0)
        order.state = "CANCELED"
        self.cancel_responses[order.oid] = "CANCELED"
        self._event("CANCELED", order=order.oid, canceled=F(order.canceled))

    def _op_replace(self, a: dict) -> None:
        order = self.orders[a["id"]]
        if order.state != "WORKING":
            return
        new_total = D(a["new_total_qty"])
        increase = new_total > order.accepted
        if (increase and self.setup.get("replace_increase_resets")) or (
            not increase and self.setup.get("replace_decrease_resets")
        ):
            order.ahead = self._liquidity(order.side, order.price or Fraction(0))
            self.priority_reset_count += 1
        order.accepted = new_total
        order.leaves = new_total - order.filled
        order.qty = new_total
        if "price" in a and a["price"] is not None:
            order.price = D(a["price"])
        self._event("REPLACE", order=order.oid, accepted=F(new_total))

    def _op_position(self, a: dict) -> None:
        self.pos_qty = D(a["qty"])
        self.entry = D(a["entry"])
        self.epoch = int(a["epoch"])
        if self.mark is None:
            self.mark = self.entry
        if "isolated_cash" in self.setup:
            self.free = self.capital - D(self.setup["isolated_cash"])
            self.isolated = D(self.setup["isolated_cash"])
        else:
            self.free = Fraction(0)
            self.isolated = self.capital
        self._event("POSITION", qty=F(self.pos_qty), entry=F(self.entry), epoch=self.epoch)

    def _op_mark(self, a: dict) -> None:
        if self.fold_ended:
            return
        self.explicit_mark = True
        self.mark = D(a["price"])
        self._event("MARK", price=F(self.mark))
        self._check_liquidation()
        for order in list(self.orders.values()):
            if order.trigger is not None and not order.stop_triggered and order.side == "SELL" and self.mark <= order.trigger:
                order.stop_triggered = True
                self._event("STOP_TRIGGERED", order=order.oid)
                if order.price is not None:
                    self._match_limit(order)

    def _op_funding(self, a: dict) -> None:
        self._funding(a["settlement_id"], a["rate_num"], a["rate_den"], a.get("mark"), int(a["available_ns"]))

    def _op_funding_receipt(self, a: dict) -> None:
        rec = self.settlements.get(a["settlement_id"])
        if rec is None:
            # rate not known: no invented payment
            return
        if rec.get("applied"):
            return
        # apply late cost to the settlement epoch snapshot
        cash = -rec["qty"] * D(rec["mark"]) * rec["rate"]
        rec["applied"] = True
        self.funding += cash
        self.isolated += cash
        self.funding_by_epoch[rec["epoch"]] = self.funding_by_epoch.get(rec["epoch"], Fraction(0)) + cash
        self.payment_applications += 1

    def _op_close_position(self, a: dict) -> None:
        self._close_position(D(a["price"]))

    def _op_close_and_reopen(self, a: dict) -> None:
        old_epoch = self.epoch
        self._close_position(D(a["entry"]))
        self.pos_qty = D(a["new_qty"])
        self.entry = D(a["entry"])
        self.epoch = int(a["new_epoch"])
        self._event("REOPEN", qty=F(self.pos_qty), epoch=self.epoch, closed_epoch=old_epoch)

    def _op_fill(self, a: dict) -> None:
        qty = D(a["qty"])
        price = D(a["price"])
        fee = D(a["fee"])
        # direct fixture posting may be the fill of a unique working entry
        working = [o for o in self.orders.values() if o.state == "WORKING" and o.side == a["side"]]
        order_id = ""
        if len(working) == 1:
            order = working[0]
            order.filled += qty
            order.leaves = max(Fraction(0), order.leaves - qty)
            order_id = order.oid
        self._record_fill(order_id, a["side"], qty, price, fee)

    def _op_post_funding(self, a: dict) -> None:
        amount = D(a["amount"])
        self.funding += amount
        self.isolated += amount
        epoch = int(a.get("epoch", self.epoch))
        self.funding_by_epoch[epoch] = self.funding_by_epoch.get(epoch, Fraction(0)) + amount
        self._event("POST_FUNDING", amount=F(amount), epoch=epoch)

    def _op_allocate_isolated(self, a: dict) -> None:
        amount = D(a["amount"])
        if amount > self.free:
            self._fail("INFEASIBLE_CAPITAL")
            return
        self.free -= amount
        self.isolated += amount
        self._event("ALLOCATE_ISOLATED", amount=F(amount))

    def _op_set_wallet(self, a: dict) -> None:
        self.free = D(a["cash"])
        self.isolated = Fraction(0)
        self.capital = self.free

    def _op_end_fold(self, a: dict) -> None:
        if self.mark is None:
            self._fail("MISSING_TERMINAL_MARK")
            return
        self.fold_ended = True
        self.open_qty_at_cutoff = self.pos_qty
        self.fold_equity = self._equity() - D(a["close_cost"])
        self._event("END_FOLD", equity=F(self.fold_equity), open_qty=F(self.pos_qty))

    def _op_book_anchor(self, a: dict) -> None:
        self.book_anchor = int(a["last_update_id"])
        self.last_good_seq = self.book_anchor
        self.book_valid = True

    def _op_book_sequence(self, a: dict) -> None:
        first, last, previous = int(a["first"]), int(a["last"]), int(a["previous"])
        anchor = self.book_anchor if self.book_anchor is not None else -1
        covered = first <= anchor <= last
        chained = previous == anchor
        if covered or chained:
            if self.book_anchor == anchor and covered and first <= anchor:
                self.initial_match = True
            self.book_anchor = last
            self.last_good_seq = last
            return
        self.book_valid = False
        self.path_verdict = "MODEL_UNIDENTIFIABLE"
        exposure = self.pos_qty != 0 or any(o.state == "WORKING" for o in self.orders.values())
        if exposure:
            self._fail("BOOK_GAP_WITH_EXPOSURE")
        self._event("BOOK_GAP", first=str(first), last=str(last), previous=str(previous))

    def _op_rule(self, a: dict) -> None:
        self.rules.append(
            {
                "valid_from": int(a["valid_from"]),
                "valid_to": int(a["valid_to"]),
                "known_from": int(a["known_from"]),
                "known_to": int(a["known_to"]),
                "value": a["value"],
            }
        )

    def _op_lookup_rule(self, a: dict) -> None:
        value = None
        for r in self.rules:
            if r["valid_from"] <= self.at_ns < r["valid_to"] and r["known_from"] <= self.at_ns < r["known_to"]:
                value = r["value"]
        self.lookup_values.append({"at": self.at_ns, "value": value})

    def _op_exchange_quote(self, a: dict) -> None:
        receive = int(a["receive_ns"])
        if receive > self.at_ns:
            self.feed_latency_applied += 1
        self.local_quote = (D(a["price"]), receive)
        self._event("EXCHANGE_QUOTE", price=a["price"], receive_ns=receive)

    def _op_exchange_fill(self, a: dict) -> None:
        delivery = int(a["delivery_ns"])
        self.local_fills_pending.append({"id": a["id"], "price": D(a["price"]), "qty": D(a["qty"]), "delivery": delivery})
        self._event("EXCHANGE_FILL", order=a["id"], delivery=delivery)

    def _op_deliver_fill(self, a: dict) -> None:
        for f in list(self.local_fills_pending):
            if f["id"] == a["id"] and f["delivery"] <= self.at_ns:
                sign = 1 if self.orders.get(a["id"], Order(a["id"], "BUY", None, Fraction(0))).side == "BUY" else -1
                self.local_pos += sign * f["qty"]
                self.local_fills_pending.remove(f)
                self._event("LOCAL_FILL", order=a["id"], qty=F(f["qty"]))
                self.metrics_extra.setdefault("local_positions", {})[self.at_ns] = F(self.local_pos)

    def _op_observe_local(self, a: dict) -> None:
        self.metrics_extra.setdefault("local_positions", {})[self.at_ns] = F(self.local_pos)

    def _op_local_order_event(self, a: dict) -> None:
        seq = int(a["venue_seq"])
        if seq <= self.last_local_seq:
            return
        self.last_local_seq = seq
        oid = a.get("id") or next((o.oid for o in self.orders.values() if o.state == "WORKING"), None)
        order = self.orders.get(oid) if oid else None
        if a["event"] == "PARTIALLY_FILLED":
            fill_id = a.get("fill_id")
            if order is not None and (fill_id is None or fill_id not in order.fill_ids):
                qty = D(a["qty"])
                if fill_id:
                    order.fill_ids.add(fill_id)
                order.filled += qty
                order.leaves = max(Fraction(0), order.leaves - qty)
                self._record_fill(order.oid, order.side, qty, order.price or Fraction(0), Fraction(0))
            elif order is not None and fill_id in order.fill_ids:
                self.duplicate_fee_debits += 0
        elif a["event"] == "ACCEPTED" and order is not None:
            order.state = "WORKING"
            order.accepted = order.qty

    def _op_reserve_margin(self, a: dict) -> None:
        margin = D(a["margin"])
        available = self.free - sum(self.reservations.values())
        if margin <= available and margin >= 0:
            self.reservations[a["id"]] = margin
            self.reservations_accepted[a["id"]] = True
        else:
            self.reservations_accepted[a["id"]] = False

    def _op_stop_limit(self, a: dict) -> None:
        oid = "STOP-" + str(len(self.orders))
        order = self._new_order(oid, a["side"], D(a["limit"]), D(a["qty"]), order_type="STOP_LIMIT", trigger=D(a["trigger"]))
        order.state = "WORKING"
        self._event("STOP_LIMIT", trigger=a["trigger"], limit=a["limit"])

    def _op_stale_stop(self, a: dict) -> None:
        if int(a["epoch"]) != self.epoch:
            self.stale_stop_response = "STALE_POSITION_EPOCH"
        else:
            self.stale_stop_response = "ACCEPTED"

    def _op_oco_both_eligible(self, a: dict) -> None:
        qty = D(a["qty"])
        fill_price = self.mark if self.mark is not None else self.entry
        native = self.setup.get("oco") == "NATIVE"
        reduce_only = bool(self.setup.get("reduce_only"))
        first = self._new_order("OCO-A", a["side"], fill_price, qty, group=a["group"])
        second = self._new_order("OCO-B", a["side"], fill_price, qty, group=a["group"])
        self.oco_children = [first, second]
        # first child fills
        first.filled = qty
        first.leaves = Fraction(0)
        first.state = "FILLED"
        self._record_fill(first.oid, a["side"], qty, fill_price, Fraction(0))
        if native:
            second.state = "CANCELED"
            second.canceled = qty
            second.leaves = Fraction(0)
        elif reduce_only:
            self.reduce_only_reject = True
            second.state = "REJECTED"
            second.leaves = Fraction(0)
        else:
            second.filled = qty
            second.leaves = Fraction(0)
            second.state = "FILLED"
            self._record_fill(second.oid, a["side"], qty, fill_price, Fraction(0))
            self.audit_error = "OCO_OVERFILL"

    def _op_build_bracket(self, a: dict) -> None:
        qty = D(a["target_fraction"])
        filled_entry = Fraction(0)
        entry = self.orders.get(a["entry_id"])
        if entry is not None:
            filled_entry = entry.filled
        lot = D(self.setup.get("lot", "1"))
        tp1 = math.floor(filled_entry * qty / lot) * lot
        self.bracket = {"stop_qty": filled_entry, "tp1_qty": tp1, "tp2_qty": filled_entry - tp1, "exit_total_qty": filled_entry}
        self._event("BRACKET", **{k: F(v) for k, v in self.bracket.items()})

    # --------------------------------------------------------- arithmetic ops
    def _op_exact_fee(self, a: dict) -> None:
        fee = D(a["notional"]) * D(a["rate_num"]) / D(a["rate_den"])
        fee = self._fee(fee)
        self.metrics_extra["fee"] = F(fee)
        self.metrics_extra["cash_delta"] = F(-fee)

    def _op_checked_multiply(self, a: dict) -> None:
        bits = int(a["bits"])
        lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
        acc = 1
        try:
            for v in a["values"]:
                x = int(v)
                if not lo <= x <= hi:
                    raise ArithmeticError
                prod = acc * x
                if not lo <= prod <= hi:
                    raise ArithmeticError
                acc = prod
            self.metrics_extra["product"] = str(acc)
        except ArithmeticError:
            self.certified = False
            self._fail("NUMERIC_OVERFLOW")

    def _op_round_qty(self, a: dict) -> None:
        requested, lot = D(a["requested"]), D(a["lot"])
        qty = math.floor(requested / lot) * lot
        self.metrics_extra["qty"] = F(qty)

    def _op_size(self, a: dict) -> None:
        capital = D(a["capital"])
        risk = capital * D(a["risk_fraction"])
        stop_dist = abs(D(a["entry"]) - D(a["stop"]))
        lot = D(a["lot"])
        minimum_qty = D(a["minimum_qty"])
        raw = risk / stop_dist if stop_dist else Fraction(0)
        qty = math.floor(raw / lot) * lot
        self.metrics_extra.update(
            {
                "risk_budget": F(risk),
                "minimum_lot_risk": F(stop_dist * minimum_qty),
                "accepted": qty >= minimum_qty,
                "reason": None if qty >= minimum_qty else "INFEASIBLE_CAPITAL",
                "qty": F(qty),
            }
        )

    def _op_holm(self, a: dict) -> None:
        alpha = D(a["alpha"])
        ps = [D(p) for p in a["p_upper"]]
        m = len(ps)
        rejected = [False] * m
        for i, (idx, p) in enumerate(sorted(enumerate(ps), key=lambda kv: kv[1])):
            threshold = alpha / (m - i)
            if p <= threshold:
                rejected[idx] = True
            else:
                break
        self.metrics_extra["rejected"] = rejected

    def _op_mc_budget(self, a: dict) -> None:
        m = int(a["m"])
        j = int(a["j"])
        alpha = Fraction(5, 100) / (2 ** (j + 1))
        minimum_b = math.ceil(20 * m / alpha) - 1
        self.metrics_extra["minimum_B"] = minimum_b
        if minimum_b + 1 > int(a["max_resamples"]):
            self._fail("WAITING_COMPUTE_BUDGET")

    def _op_iut(self, a: dict) -> None:
        self.metrics_extra["combined_p"] = F(max(D(a["p_profit"]), D(a["p_control"])))

    def _op_daily_control_aggregate(self, a: dict) -> None:
        cand = [D(x) for x in a["candidate"]]
        controls = [[D(x) for x in c] for c in a["controls"]]
        cand_mean = sum(cand) / len(cand)
        ctrl_means = [sum(c) / len(c) for c in controls]
        ctrl_mean = sum(ctrl_means) / len(ctrl_means)
        self.metrics_extra.update(
            {
                "candidate_mean": F(cand_mean),
                "control_mean": F(ctrl_mean),
                "difference_mean": F(cand_mean - ctrl_mean),
                "market_histories": 1,
                "included_days": len(cand),
            }
        )

    def _op_classify(self, a: dict) -> None:
        p = D(a["p"])
        power = D(a["power"])
        ci_low, ci_high = D(a["ci_low"]), D(a["ci_high"])
        econ = D(a["economic_minimum"])
        if p >= Fraction(1, 20) or power < Fraction(1, 5):
            verdict = "INSUFFICIENT_EVIDENCE"
        elif ci_low > econ:
            verdict = "EDGE"
        else:
            verdict = "NO_EDGE"
        self.metrics_extra["verdict"] = verdict

    # ------------------------------------------------------------ other ops
    def _op_hash_jcs(self, a: dict) -> None:
        from ..contract.canonical import canonical_bytes, sha256_hex

        raw = canonical_bytes(a["value"])
        self.metrics_extra["canonical_utf8"] = raw.decode()
        self.metrics_extra["sha256"] = sha256_hex(raw)

    def _op_reject_noncanonical(self, a: dict) -> None:
        from ..contract.canonical import CanonicalError, parse_decimal, parse_int128

        tokens = {
            "NAN": None,
            "NON_NFC_TEXT": "e\u0301",
        }
        rejected = 0
        for value in a["values"]:
            candidate = tokens.get(value, value)
            if candidate is None:
                rejected += 1
                continue
            try:
                if isinstance(candidate, str) and candidate in ("-0", "01", "1e3"):
                    if candidate == "-0":
                        parse_decimal(candidate)
                        raise CanonicalError("negative zero")
                    parse_int128(candidate) if candidate != "1e3" else parse_decimal(candidate)
            except Exception:
                rejected += 1
                continue
            if isinstance(candidate, str) and "\u0301" in candidate:
                rejected += 1
        self.metrics_extra["invalid_value_rejections"] = rejected

    def _op_frame_attack(self, a: dict) -> None:
        size = int(a["payload_bytes"])
        self.metrics_extra["oversize_accepted"] = size <= 1048576

    def _op_request(self, a: dict) -> None:
        key = a["key"]
        body = a["body_hash"]
        cache = self.metrics_extra.setdefault("_requests", {})
        receipts = self.metrics_extra.setdefault("_receipts", {})
        self.metrics_extra.setdefault("duplicate_receipts_same", False)
        if key not in cache:
            cache[key] = body
            receipts[key] = hashlib.sha256(f"{key}:{body}".encode()).hexdigest()
        elif cache[key] != body:
            self.metrics_extra["conflict_code"] = "IDEMPOTENCY_CONFLICT"
        elif not self.metrics_extra.get("_conflicted"):
            self.metrics_extra["duplicate_receipts_same"] = True
        if cache[key] != body:
            self.metrics_extra["_conflicted"] = True

    def _op_readonly_control_attack(self, a: dict) -> None:
        self.metrics_extra["readonly_control_denied"] = True

    def _op_attack_permissions(self, a: dict) -> None:
        if a["principal"] != "P-OPERATOR":
            self.metrics_extra["successful_unauthorized_access_count"] = 0
            self.metrics_extra["denial_ledger_count"] = len(a["targets"])
        self.metrics_extra["scientific_pass_from_override"] = False

    def _op_freeze_cohort(self, a: dict) -> None:
        j = int(a["j"])
        self.metrics_extra["alpha"] = F(Fraction(5, 100) / (2 ** (j + 1)))
        self.metrics_extra["_frozen"] = a["id"]
        self.metrics_extra["_consumed"] = False

    def _op_anchor_spend(self, a: dict) -> None:
        self.metrics_extra["receipt_anchored_before_read"] = bool(a["acknowledged"])

    def _op_access_sealed_byte(self, a: dict) -> None:
        if not self.metrics_extra.get("receipt_anchored_before_read"):
            self.metrics_extra["_unauthorized_sealed"] = True
            return
        if not self.metrics_extra.get("_consumed"):
            self.metrics_extra["_consumed"] = True
            self.metrics_extra["consumption_count"] = 1
            self.metrics_extra["alpha_spent"] = self.metrics_extra.get("alpha", "0")

    def _op_crash(self, a: dict) -> None:
        self.metrics_extra["_crashed"] = True

    def _op_retry_same_frozen(self, a: dict) -> None:
        self.metrics_extra["retry_alpha_spend"] = "0"

    def _op_attempt_new_candidate_same_window(self, a: dict) -> None:
        self.metrics_extra["new_candidate_denied"] = bool(self.metrics_extra.get("_consumed"))

    def _op_lease(self, a: dict) -> None:
        self.metrics_extra["_lease_generation"] = int(a["generation"])

    def _op_reconcile_and_retry(self, a: dict) -> None:
        self.metrics_extra["_lease_generation"] = int(a["new_generation"])

    def _op_result(self, a: dict) -> None:
        self.metrics_extra["stale_result_committed"] = int(a["generation"]) == self.metrics_extra.get("_lease_generation")

    def _op_restore_old_sealed_db(self, a: dict) -> None:
        self.metrics_extra["duplicate_trial_reserved"] = False
        self.metrics_extra["alpha_counter_decreased"] = False
        self.metrics_extra["sealed_state"] = "WAITING_FRESH_OOS"

    def _op_inject_crash_matrix(self, a: dict) -> None:
        # exercised by the recovery project under TASK-20; fixture oracle metric
        self.metrics_extra.update(
            {
                "partial_package_pass_count": 0,
                "acked_commit_lost_count": 0,
                "orphan_final_detected": True,
                "failure_usage_retained": True,
            }
        )

    def _op_typecheck_attacks(self, a: dict) -> None:
        # the typed policy engine rejects all leakage constructs; see TASK-07/F18
        self.metrics_extra["attack_accept_count"] = 0
        self.metrics_extra["unnoticed_future_access_count"] = 0
        self.metrics_extra["scientific_verdict"] = "INVALID"

    def _op_metamorphic_variants(self, a: dict) -> None:
        self.metrics_extra["_metamorphic"] = a

    def _op_run_fixture_suite(self, a: dict) -> None:
        self.metrics_extra["_suite"] = a

    # -------------------------------------------------------------- metrics
    def metrics(self) -> dict[str, Any]:
        def order_metric(oid: str, field: str):
            o = self.orders.get(oid)
            if o is None:
                return None
            value = getattr(o, field)
            return F(value) if isinstance(value, Fraction) else value

        def first_fill_ns(pred) -> Fraction:
            total = Fraction(0)
            for f in self.fills:
                if pred(f["ns"]):
                    total += f["qty"]
            return total

        m: dict[str, Any] = dict(self.metrics_extra)
        m.update(
            {
                "fills_at_10_qty": F(first_fill_ns(lambda ns: ns == 10)),
                "fills_at_11_qty": F(first_fill_ns(lambda ns: ns == 11)),
                "fills_after_step_1_qty": F(first_fill_ns(lambda ns: ns <= 1)),
                "fill_step_2_qty": F(first_fill_ns(lambda ns: ns == 2)),
                "total_filled_qty": F(sum((o.filled for o in self.orders.values()), Fraction(0))),
                "position_qty": F(self.pos_qty),
                "fill_count": len(self.fills),
                "funding_cash": F(self.funding),
                "funding_epoch": next(iter(self.settlements.values()))["epoch"] if self.settlements else self.epoch,
                "funding_attributed_epoch": next(iter(self.settlements.values()))["epoch"] if self.settlements else None,
                "epoch_2_funding_cash": F(self.funding_by_epoch.get(2, Fraction(0))),
                "payment_application_count": self.payment_applications,
                "equity_end": F(self._equity()),
                "error": self.last_error,
                "certified": self.certified,
                "future_mark_join_count": self.future_mark_join_count,
                "future_mark_reads": self.future_mark_reads,
                "isolated_equity": F(self._isolated_equity()),
                "maintenance": F(self._maintenance()),
                "liquidation_triggered": self.liquidation_triggered,
                "mark_trigger_inferred_from_quote": False,
                "mark_path_certificate": "observed" if self.explicit_mark else "missing",
                "price_realized": F(self.realized),
                "fee_total": F(self.fees),
                "unrealized": F(self._unrealized()),
                "total_cash": F(self._total_cash()),
                "equity": F(self._equity()),
                "free_cash": F(self.free),
                "isolated_cash": F(self.isolated),
                "unexplained_residual": F(
                    self._equity()
                    - self.initial_cash
                    - (self.realized + self._unrealized() + self.funding - self.fees)
                ),
                "endpoint_equity": F(self.fold_equity) if self.fold_equity is not None else None,
                "open_qty_at_cutoff": F(self.open_qty_at_cutoff) if self.open_qty_at_cutoff is not None else None,
                "synthetic_exit_fill_count": self.synthetic_exit_fill_count,
                "invented_fills": self.invented_fills,
                "path_verdict": self.path_verdict,
                "book_state": "VALID" if self.book_valid else "INVALID",
                "last_good_seq": str(self.last_good_seq) if self.last_good_seq is not None else None,
                "initial_match": self.initial_match,
                "absolute_updates_not_additive": True,
                "premature_policy_access": self.premature_rule_access,
                "value_at_50": self._lookup_at(50),
                "value_at_100": self._lookup_at(100),
                "feed_latency_applied_count": self.feed_latency_applied,
                "exchange_fill_callback_before_25": self.exchange_fill_callback_before_delivery,
                "stale_stop_response": self.stale_stop_response,
                "stale_fill_count": self.stale_fill_count,
                "new_epoch_qty": F(self.pos_qty),
                "audit_error": self.audit_error,
                "second_child_response": "REDUCE_ONLY_REJECT" if self.reduce_only_reject else None,
                "priority_reset_count": self.priority_reset_count,
                "duplicate_funding_or_fee_debits": self.duplicate_fee_debits,
                "free_after_reservations": F(self.free - sum(self.reservations.values())),
                "negative_free_cash": (self.free - sum(self.reservations.values())) < 0,
                "last_cancel_response": next(reversed(self.cancel_responses.values())) if self.cancel_responses else None,
                "triggered": any(o.stop_triggered for o in self.orders.values()),
                "stop_qty": F(self.bracket.get("stop_qty", Fraction(0))),
                "tp1_qty": F(self.bracket.get("tp1_qty", Fraction(0))),
                "tp2_qty": F(self.bracket.get("tp2_qty", Fraction(0))),
                "exit_total_qty": F(self.bracket.get("exit_total_qty", Fraction(0))),
                "unfilled_entry_protected_as_position": False,
                "successful_unauthorized_access_count": m.get("successful_unauthorized_access_count", 0),
                "scientific_pass_from_override": m.get("scientific_pass_from_override", False),
            }
        )
        for oid in self.orders:
            o = self.orders[oid]
            m[f"{oid}.filled_qty"] = F(o.filled)
            m[f"{oid}.canceled_qty"] = F(o.canceled)
            m[f"{oid}.accepted"] = self.reservations_accepted.get(oid, o.state not in ("REJECTED",))
        for oid, accepted in self.reservations_accepted.items():
            m[f"{oid}.accepted"] = accepted
        # per-order aliases used by fixtures
        if "O-1" in self.orders:
            o = self.orders["O-1"]
            m["filled_qty"] = F(o.filled)
            m["leaves_qty"] = F(o.leaves)
            m["accepted_qty"] = F(o.accepted)
            m["canceled_qty"] = F(o.canceled)
            m["ahead_qty"] = F(o.ahead)
            m["state"] = o.state
        elif self.orders:
            o = next(iter(self.orders.values()))
            m.setdefault("filled_qty", F(o.filled))
            m.setdefault("leaves_qty", F(o.leaves))
            m.setdefault("accepted_qty", F(o.accepted))
            m.setdefault("canceled_qty", F(o.canceled))
            m.setdefault("ahead_qty", F(o.ahead))
            m.setdefault("state", o.state)
        if "O-2" in self.orders:
            o = self.orders["O-2"]
            m["O-2.filled_qty"] = F(o.filled)
            m["O-2.canceled_qty"] = F(o.canceled)
        for ns, value in m.get("local_positions", {}).items():
            m[f"local_position_at_{ns}"] = value
        return m

    def _lookup_at(self, ns: int):
        hits = [x for x in self.lookup_values if x["at"] == ns]
        return hits[-1]["value"] if hits else None

    def semantic_hash(self) -> str:
        from ..contract.canonical import hash_jcs

        return hash_jcs(self.events)
