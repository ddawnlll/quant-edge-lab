"""Independent exact accounting reconstructor.

This module consumes an economic event tape and rebuilds cash, positions and
journal postings without importing the native matching engine. It is the
C-ACCOUNT oracle, intentionally written in a different language/stack from
the Rust native runner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

CURRENCY_EXPONENT = 2


class AccountingError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def dec(value: str | int | Fraction) -> Fraction:
    if isinstance(value, Fraction):
        return value
    return Fraction(str(value))


def fmt(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    scale = 0
    while scale <= 18:
        if (10**scale) % value.denominator == 0:
            n = value.numerator * ((10**scale) // value.denominator)
            sign = "-" if n < 0 else ""
            digits = str(abs(n)).rjust(scale + 1, "0")
            frac = digits[-scale:].rstrip("0")
            whole = digits[:-scale]
            return sign + whole + ("." + frac if frac else "")
        scale += 1
    raise AccountingError("NUMERIC_OVERFLOW", "non-terminating currency value")


def money(value: Fraction, exponent: int = CURRENCY_EXPONENT) -> Fraction:
    q = Fraction(10) ** exponent
    return Fraction(round(value * q)) / q


@dataclass
class Posting:
    account_code: str
    currency_id: str
    debit_i: str
    credit_i: str
    position_epoch: str | None
    strategy_id: str | None

    def as_dict(self) -> dict:
        return {
            "account_code": self.account_code,
            "currency_id": self.currency_id,
            "debit_i": self.debit_i,
            "credit_i": self.credit_i,
            "position_epoch": self.position_epoch,
            "strategy_id": self.strategy_id,
        }


@dataclass
class Account:
    currency: str = "USDT"
    free: Fraction = Fraction(0)
    isolated: Fraction = Fraction(0)
    position_qty: Fraction = Fraction(0)
    entry: Fraction = Fraction(0)
    mark: Fraction | None = None
    epoch: int = 1
    realized_physical: Fraction = Fraction(0)
    realized_fifo: Fraction = Fraction(0)
    fees: Fraction = Fraction(0)
    funding: Fraction = Fraction(0)
    liquidation_cost: Fraction = Fraction(0)
    external_net: Fraction = Fraction(0)
    start_cash: Fraction = Fraction(0)
    lots: list[list[Fraction]] = field(default_factory=list)  # [qty, price]
    postings: list[Posting] = field(default_factory=list)
    funding_epochs: dict[int, Fraction] = field(default_factory=dict)
    funding_ids: set[tuple[str, int]] = field(default_factory=set)
    reservations: dict[str, Fraction] = field(default_factory=dict)
    close_provision: Fraction = Fraction(0)
    terminal: bool = False
    last_ts: int | None = None
    epoch_windows: dict[int, dict[str, int | None]] = field(default_factory=dict)

    def _post(self, debit: str, credit: str, amount: Fraction, epoch: int | None = None) -> None:
        if amount < 0:
            debit, credit = credit, debit
            amount = -amount
        coeff = str(int(round(amount * (10**CURRENCY_EXPONENT))))
        self.postings.append(
            Posting(debit, self.currency, coeff, "0", str(epoch) if epoch else None, "STRAT-1")
        )
        self.postings.append(
            Posting(credit, self.currency, "0", coeff, str(epoch) if epoch else None, "STRAT-1")
        )

    def set_position(self, qty: Fraction, entry: Fraction, epoch: int) -> None:
        """Bootstrap a position from a fixture/native snapshot; no cash move."""
        self.position_qty = qty
        self.entry = entry
        self.epoch = epoch
        self.mark = entry if self.mark is None else self.mark
        self.lots = [[qty, entry]] if qty != 0 else []

    def set_wallet(self, cash: Fraction) -> None:
        self.free = cash
        self.start_cash = cash
        self._post("CASH_FREE", "EXTERNAL_CAPITAL", cash)

    def allocate_isolated(self, amount: Fraction) -> None:
        if amount > self.free:
            raise AccountingError("INFEASIBLE_CAPITAL")
        self.free -= amount
        self.isolated += amount
        self._post("CASH_ISOLATED", "CASH_FREE", amount)

    def reserve(self, reservation_id: str, margin: Fraction) -> bool:
        available = self.free - sum(self.reservations.values(), Fraction(0))
        if margin < 0 or margin > available:
            return False
        self.reservations[reservation_id] = margin
        return True

    def fill(self, side: str, qty: Fraction, price: Fraction, fee: Fraction) -> None:
        if qty <= 0:
            raise AccountingError("INVALID_DATA", "qty must be positive")
        signed = qty if side == "BUY" else -qty
        realized = Fraction(0)
        if self.position_qty == 0 or (self.position_qty > 0) == (signed > 0):
            new_qty = self.position_qty + signed
            self.entry = price if self.position_qty == 0 else (
                self.entry * abs(self.position_qty) + price * qty
            ) / abs(new_qty)
            self.position_qty = new_qty
        else:
            old = self.position_qty
            close_qty = min(abs(old), qty)
            realized = (price - self.entry) * close_qty if old > 0 else (self.entry - price) * close_qty
            self.realized_physical += realized
            self.position_qty = old + signed
            if self.position_qty == 0:
                self.entry = Fraction(0)
                self.epoch += 1
            elif (self.position_qty > 0) != (old > 0):
                self.entry = price
                self.epoch += 1
        self._fifo(signed, price)
        self.isolated += realized - fee
        self.fees += fee
        self._post("CASH_ISOLATED", "REALIZED_PRICE_PNL", realized, self.epoch)
        self._post("FEES", "CASH_ISOLATED", fee, self.epoch)

    def _fifo(self, signed: Fraction, price: Fraction) -> None:
        remaining = signed
        while remaining != 0 and self.lots:
            lot = self.lots[0]
            if (lot[0] > 0) == (remaining > 0):
                break
            take = min(abs(lot[0]), abs(remaining))
            direction = 1 if lot[0] > 0 else -1
            pnl = (price - lot[1]) * take * direction
            self.realized_fifo += pnl
            lot[0] -= direction * take
            remaining += direction * take
            if abs(lot[0]) < Fraction(1, 10**18):
                self.lots.pop(0)
        if remaining != 0:
            self.lots.append([remaining, price])

    def funding_payment(self, amount: Fraction, epoch: int, settlement_id: str | None = None) -> None:
        if settlement_id is not None:
            key = (settlement_id, epoch)
            if key in self.funding_ids:
                raise AccountingError("PROTOCOL_VIOLATION", "duplicate funding settlement")
            self.funding_ids.add(key)
        self.funding += amount
        self.isolated += amount
        self.funding_epochs[epoch] = self.funding_epochs.get(epoch, Fraction(0)) + amount
        self._post("FUNDING", "CASH_ISOLATED", amount, epoch)

    def note_epoch_open(self, epoch: int, ts: int | None) -> None:
        window = self.epoch_windows.setdefault(epoch, {"open_ts": None, "close_ts": None})
        if window["open_ts"] is None and ts is not None:
            window["open_ts"] = ts

    def note_epoch_close(self, epoch: int, ts: int | None) -> None:
        window = self.epoch_windows.setdefault(epoch, {"open_ts": None, "close_ts": None})
        if ts is not None:
            window["close_ts"] = ts

    def set_mark(self, price: Fraction) -> None:
        self.mark = price

    def unrealized(self) -> Fraction:
        if self.position_qty == 0 or self.mark is None:
            return Fraction(0)
        return self.position_qty * (self.mark - self.entry)

    def equity(self) -> Fraction:
        return self.free + self.isolated + self.unrealized()

    def end_fold(self, close_cost: Fraction) -> None:
        self.close_provision = close_cost
        self.terminal = True

    def residual(self) -> Fraction:
        return self.equity() - self.start_cash - self.external_net - (
            self.realized_physical + self.unrealized() + self.funding - self.fees - self.liquidation_cost
        )

    def snapshot(self) -> dict:
        return {
            "free_cash": fmt(self.free),
            "isolated_cash": fmt(self.isolated),
            "price_realized": fmt(self.realized_physical),
            "fifo_realized": fmt(self.realized_fifo),
            "fee_total": fmt(self.fees),
            "funding_cash": fmt(self.funding),
            "funding_by_epoch": {
                str(epoch): fmt(amount)
                for epoch, amount in sorted(self.funding_epochs.items())
            },
            "funding_settlement_count": len(self.funding_ids),
            "unrealized": fmt(self.unrealized()),
            "position_qty": fmt(self.position_qty),
            "total_cash": fmt(self.free + self.isolated),
            "equity": fmt(self.equity()),
            "endpoint_equity": fmt(self.equity() - self.close_provision) if self.terminal else None,
            "unexplained_residual": fmt(self.residual()),
            "reserved": fmt(sum(self.reservations.values(), Fraction(0))),
            "free_after_reservations": fmt(self.free - sum(self.reservations.values(), Fraction(0))),
            "negative_free_cash": (self.free - sum(self.reservations.values(), Fraction(0))) < 0,
            "postings": [p.as_dict() for p in self.postings],
        }


def _epoch_windows(tape: list[dict[str, Any]]) -> dict[int, dict[str, int | None]]:
    """Replay fills only to derive each position epoch's live time window."""
    probe = Account()
    for event in tape:
        kind = event["type"]
        if kind == "SET_POSITION":
            epoch = int(event.get("epoch", 1))
            probe.set_position(dec(event["qty"]), dec(event["entry"]), epoch)
            if probe.position_qty != 0:
                ts = event.get("ts_ns")
                probe.note_epoch_open(epoch, int(ts) if ts is not None else None)
        elif kind == "FILL":
            before_qty = probe.position_qty
            before_epoch = probe.epoch
            probe.fill(
                event["side"],
                dec(event["qty"]),
                dec(event["price"]),
                dec(event.get("fee", "0")),
            )
            ts = event.get("ts_ns")
            fill_ts = int(ts) if ts is not None else None
            if before_qty == 0 and probe.position_qty != 0:
                probe.note_epoch_open(probe.epoch, fill_ts)
            elif before_qty != 0 and probe.position_qty == 0:
                probe.note_epoch_close(before_epoch, fill_ts)
            elif before_qty != 0 and probe.position_qty != 0 and probe.epoch != before_epoch:
                probe.note_epoch_close(before_epoch, fill_ts)
                probe.note_epoch_open(probe.epoch, fill_ts)
    return probe.epoch_windows


def reconstruct(tape: list[dict[str, Any]], currency: str = "USDT") -> dict:
    acct = Account(currency=currency)
    acct.epoch_windows = _epoch_windows(tape)

    def observe_ts(event: dict[str, Any]) -> None:
        ts = event.get("ts_ns")
        if ts is None:
            return
        value = int(ts)
        if acct.last_ts is not None and value < acct.last_ts:
            raise AccountingError(
                "OUT_OF_ORDER",
                f"event ts_ns {value} precedes previous ts_ns {acct.last_ts}",
            )
        acct.last_ts = value

    for event in tape:
        kind = event["type"]
        if kind == "SET_WALLET":
            acct.set_wallet(dec(event["cash"]))
        elif kind == "SET_POSITION":
            epoch = int(event.get("epoch", 1))
            acct.set_position(dec(event["qty"]), dec(event["entry"]), epoch)
        elif kind == "ALLOCATE_ISOLATED":
            acct.allocate_isolated(dec(event["amount"]))
        elif kind == "RESERVE":
            acct.reserve(event["id"], dec(event["margin"]))
        elif kind == "FILL":
            observe_ts(event)
            acct.fill(event["side"], dec(event["qty"]), dec(event["price"]), dec(event.get("fee", "0")))
        elif kind == "FUNDING":
            if acct.terminal:
                raise AccountingError("PROTOCOL_VIOLATION", "funding posting after END_FOLD")
            epoch = int(event.get("epoch", acct.epoch))
            window = acct.epoch_windows.get(epoch)
            # F09 late funding: a receipt may arrive after the position has
            # rolled to a later epoch. The cash is attributed to the referenced
            # (older) epoch, never to the current short/long position.
            if epoch > acct.epoch or window is None:
                raise AccountingError(
                    "PROTOCOL_VIOLATION",
                    f"funding references epoch {epoch} with no open position history",
                )
            ts = event.get("ts_ns")
            if ts is not None:
                value = int(ts)
                open_ts = window.get("open_ts")
                close_ts = window.get("close_ts")
                if (open_ts is not None and value < open_ts) or (
                    close_ts is not None and value > close_ts
                ):
                    raise AccountingError(
                        "OUT_OF_ORDER",
                        f"funding ts_ns {value} is outside epoch {epoch} window "
                        f"[{open_ts}, {close_ts}]",
                    )
            acct.funding_payment(dec(event["amount"]), epoch, event.get("settlement_id"))
        elif kind == "MARK":
            observe_ts(event)
            acct.set_mark(dec(event["price"]))
        elif kind == "END_FOLD":
            observe_ts(event)
            acct.end_fold(dec(event.get("close_cost", "0")))
        else:
            raise AccountingError("INVALID_DATA", f"unknown tape event {kind}")
    result = acct.snapshot()
    result["postings_balanced"] = all(
        int(p.debit_i) >= 0 and int(p.credit_i) >= 0 for p in acct.postings
    )
    return result


def audit(snapshot: dict, tape: list[dict[str, Any]]) -> dict:
    rebuilt = reconstruct(tape)
    native_equity = dec(snapshot.get("equity", "0"))
    residual = dec(rebuilt["unexplained_residual"])
    return {
        "status": "PASS" if residual == 0 and native_equity == dec(rebuilt["equity"]) else "FAIL",
        "rebuilt_equity": rebuilt["equity"],
        "native_equity": fmt(native_equity),
        "residual": rebuilt["unexplained_residual"],
        "journal": rebuilt["postings"],
    }
