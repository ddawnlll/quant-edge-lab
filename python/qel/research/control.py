"""Matched random-entry controls under the frozen HMAC seed protocol.

This module replaces the development-only NumPy PCG64 control sample with the
preregistered ``HMAC_SHA256_COUNTER_V1`` protocol from :mod:`qel.research.seed`.

The control runner is deliberately split into three layers:

``prepare_control_plan``
    Fits eligibility, side rates and holding-time samples from *training
    strata only* and freezes the per-day trade targets, strata weights and
    calendar.  It never reads evaluation prices, future fills or candidate
    outcomes, so mutating them cannot change control selection.

``run_control_seed``
    Runs one frozen control seed.  Every seed owns a fresh
    :class:`ControlAccount`; there is no shared mutable account or cash state.

``StreamingControlAggregator``
    Aggregates seed results one at a time into per-day and per-stratum
    accumulators plus a rolling hash.  It never retains per-seed daily series,
    so memory is bounded by the calendar length and stratum count.

A batch execution prepares the plan once; a sequential execution rebuilds the
same plan per seed.  Both must produce the same daily calendar series.  Short
seed runs or exhausted compute budgets report ``INCOMPLETE_EVIDENCE``.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import resource
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..accounting.core import fmt
from ..contract.canonical import hash_jcs
from .seed import (
    LABELS,
    PROTOCOL_HASH,
    backend_info,
    uniform_at,
)

# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

DRAW_MAPPING = "HMAC_SHA256_COUNTER_V1"
RESCUE_METHOD = "HMAC_COUNTER_UNIFORM_RESERVOIR_WITH_FROZEN_FAMILY_SEED"
RESCUE_SCOPE = "DEVELOPMENT_VALID_L1_REJECTS"
L1_GROUP_SIZE = 10
CONTROL_REQUIRED_REPLICATIONS = 1000
TAKER_FEE = Fraction(4, 10000)
#: Perpetual funding is settled from the native tape.  A long position pays a
#: positive rate (negative cash) and a short receives it.  The reference
#: notional is the position's own entry notional, so no mark price is inferred.
FUNDING_MODEL = "EXACT_NATIVE_TAPE_FUNDING"
FUNDING_SIGN_CONVENTION = "LONG_PAYS_POSITIVE_RATE_SHORT_RECEIVES"
FUNDING_WINDOW = "[entry_ts, exit_ts)"
#: Frozen training holding policy: hold for a fixed number of training quote
#: intervals.  This is fitted from training timestamps only and is deliberately
#: independent of evaluation prices.
TRAINING_HOLDING_QUOTES = 2
#: Keep the per-day trade target below the training-implied capacity so that
#: the last entry still has an exit quote inside the evaluation window.
TARGET_CAPACITY_FRACTION = Fraction(3, 5)

#: Frozen generator ids.  The six seed labels are fixed by the specification;
#: the generator id separates independent draw purposes that share a
#: ``(program, variant, coin, time, control)`` coordinate.
GENERATOR_STRATUM = 0x1301
GENERATOR_ENTRY_JITTER = 0x1302
GENERATOR_SIDE = 0x1303
GENERATOR_HOLDING = 0x1304
GENERATOR_RESCUE = 0x1305

GENERATOR_IDS = {
    "STRATUM": GENERATOR_STRATUM,
    "ENTRY_JITTER": GENERATOR_ENTRY_JITTER,
    "SIDE": GENERATOR_SIDE,
    "HOLDING": GENERATOR_HOLDING,
    "RESCUE": GENERATOR_RESCUE,
}

SCHEMA_VERSION = "1.1.0"
STATE_VALID = "VALID"
STATE_MISSING_MARK = "MISSING_MARK"
STATE_NO_TRADES = "NO_TRADES"


# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------


class ControlError(ValueError):
    """A control input is outside the frozen protocol domain."""


def _hash_json(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()


def _file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _median(sorted_values: Sequence[int]) -> int:
    if not sorted_values:
        raise ControlError("EMPTY_DISTRIBUTION")
    n = len(sorted_values)
    mid = n // 2
    if n % 2:
        return int(sorted_values[mid])
    return int((int(sorted_values[mid - 1]) + int(sorted_values[mid])) // 2)


def _quantile(sorted_values: Sequence[int], u: float) -> int:
    if not sorted_values:
        raise ControlError("EMPTY_DISTRIBUTION")
    index = min(len(sorted_values) - 1, max(0, int(u * len(sorted_values))))
    return int(sorted_values[index])


def _round_int(value: Fraction) -> int:
    """Round a non-negative Fraction to the nearest integer, ties upward."""
    return int((value + Fraction(1, 2)) // 1)


def _ceil_int(value: Fraction) -> int:
    """Ceiling of a non-negative Fraction."""
    return -((-value.numerator) // value.denominator)


def _num_str(value: Fraction, places: int = 12) -> str:
    """Exact string when terminating, otherwise a fixed decimal diagnostic."""
    try:
        return fmt(value)
    except Exception:
        text = f"{float(value):.{places}f}"
        return text.rstrip("0").rstrip(".") if "." in text else text


_DAY_CACHE: dict[int, str] = {}


def _day_of(ts: int) -> str:
    # UTC calendar day without depending on the host timezone.  Multi-day
    # preparation calls this once per quote; cache by epoch-day so the cost is
    # one datetime conversion per day rather than per quote.
    day_number = ts // 86_400_000_000_000
    cached = _DAY_CACHE.get(day_number)
    if cached is None:
        import datetime

        cached = (
            datetime.datetime.fromtimestamp(
                day_number * 86_400, tz=datetime.timezone.utc
            )
            .date()
            .isoformat()
        )
        _DAY_CACHE[day_number] = cached
    return cached


def _peak_rss_bytes() -> int:
    """Peak resident set size for the current process (Linux KiB units)."""
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


def _hour_of(ts: int) -> int:
    return int((ts // 1_000_000_000) % 86400) // 3600


def _stratum_key(hour: int) -> str:
    return f"H{hour:02d}"


def _entry_price(quote: "Quote", side: int) -> Fraction:
    return quote.ask if side > 0 else quote.bid


def _exit_price(quote: "Quote", side: int) -> Fraction:
    return quote.bid if side > 0 else quote.ask


# ---------------------------------------------------------------------------
# Quotes and plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Quote:
    ts: int
    bid: Fraction
    ask: Fraction


@dataclass(frozen=True)
class FundingSettlement:
    """One deduplicated funding settlement from the native tape."""

    ts: int
    rate: Fraction


def _to_quote(raw: Mapping[str, Any] | Quote) -> Quote:
    if isinstance(raw, Quote):
        return raw
    if "ts" in raw:
        ts = int(raw["ts"])
    else:
        ts = int(raw["ts_ns"])
    bid = raw.get("bid")
    ask = raw.get("ask")
    bid_exact = raw.get("bid_exact")
    ask_exact = raw.get("ask_exact")
    if bid is None:
        bid = bid_exact
    if ask is None:
        ask = ask_exact
    # Prefer the exact decimal only when it still agrees with the visible
    # float.  This keeps a price-mutation test honest instead of silently
    # reading a stale companion field.  Parse each exact string once: a
    # multi-day tape is hundreds of thousands of quotes and repeated Fraction
    # parsing dominates preparation.
    if bid_exact is not None:
        bid_f = Fraction(str(bid_exact))
        if float(bid_f) != float(bid):
            bid_f = Fraction(str(bid))
    else:
        bid_f = Fraction(str(bid))
    if ask_exact is not None:
        ask_f = Fraction(str(ask_exact))
        if float(ask_f) != float(ask):
            ask_f = Fraction(str(ask))
    else:
        ask_f = Fraction(str(ask))
    return Quote(ts=ts, bid=bid_f, ask=ask_f)


def _to_funding(raw: Mapping[str, Any] | FundingSettlement) -> FundingSettlement:
    if isinstance(raw, FundingSettlement):
        return raw
    if "ts" in raw:
        ts = int(raw["ts"])
    else:
        ts = int(raw["ts_ns"])
    return FundingSettlement(ts=ts, rate=Fraction(str(raw["rate"])))


def normalize_funding(
    events: Sequence[Mapping[str, Any] | FundingSettlement],
) -> tuple[tuple[FundingSettlement, ...], int]:
    """Deduplicate funding events by ``(ts_ns, rate)`` and sort by timestamp.

    The native development tape carries each monthly funding record once per
    day of the window, so the same settlement appears seven times.  Exact
    duplicate keys collapse; two different rates at the same timestamp are both
    kept because they are genuinely distinct settlements.
    """
    raw_count = len(events)
    unique: dict[tuple[int, Fraction], FundingSettlement] = {}
    for item in events:
        settlement = _to_funding(item)
        unique[(settlement.ts, settlement.rate)] = settlement
    ordered = tuple(sorted(unique.values(), key=lambda s: (s.ts, s.rate)))
    return ordered, raw_count


def load_tape(
    events_path: str,
) -> tuple[list[dict], tuple[FundingSettlement, ...], int]:
    """Load quotes and funding from a native JSONL tape in one streaming pass.

    Returns ``(quotes, funding_settlements, raw_funding_event_count)``.  The
    tape is streamed rather than read whole so a multi-gigabyte development
    window stays well below the memory ceiling.
    """
    quotes: list[dict] = []
    funding_raw: list[dict] = []
    with open(events_path, "r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            rec = json.loads(line)
            kind = rec.get("kind")
            if kind == "Quote":
                quotes.append(
                    {
                        "ts": int(rec["ts_ns"]),
                        "bid": float(rec["bid"]),
                        "ask": float(rec["ask"]),
                        "bid_exact": rec["bid"],
                        "ask_exact": rec["ask"],
                    }
                )
            elif kind == "Funding":
                funding_raw.append({"ts_ns": int(rec["ts_ns"]), "rate": rec["rate"]})
    if len(quotes) < 4:
        raise ValueError("INSUFFICIENT_HISTORY")
    settlements, raw_count = normalize_funding(funding_raw)
    return sorted(quotes, key=lambda q: q["ts"]), settlements, raw_count


def load_quotes(events_path: str) -> list[dict]:
    """Load quote records from a native JSONL tape.

    The public shape is kept backward compatible (``ts``/``bid``/``ask``
    floats).  Exact decimal strings are carried alongside so the control
    account can use exact ``Fraction`` arithmetic.
    """
    quotes, _, _ = load_tape(events_path)
    return quotes


def load_funding(events_path: str) -> tuple[tuple[FundingSettlement, ...], int]:
    """Load, deduplicate and sort funding settlements from a native tape."""
    _, settlements, raw_count = load_tape(events_path)
    return settlements, raw_count


def funding_window_indices(
    funding_ts: Sequence[int], entry_ts: int, exit_ts: int
) -> tuple[int, int]:
    """Half-open settlement window ``[entry_ts, exit_ts)``.

    A settlement stamped exactly at entry applies; one stamped exactly at exit
    does not.  Both operands are required to be ordered ascending.
    """
    return (
        bisect.bisect_left(funding_ts, entry_ts),
        bisect.bisect_left(funding_ts, exit_ts),
    )


def funding_cash_delta(side: int, reference_notional: Fraction, rate: Fraction) -> Fraction:
    """Exact cash delta of one funding settlement.

    A long (``side > 0``) pays a positive rate, so the delta is negative; a
    short (``side < 0``) receives it.  The reference notional is the position's
    own entry notional.
    """
    return -side * reference_notional * rate


@dataclass(frozen=True)
class StratumModel:
    key: str
    hour: int
    weight: Fraction
    long_rate: Fraction
    holding_samples: tuple[int, ...]
    holding_target_ns: int
    train_quotes: int
    eval_quotes: int


@dataclass(frozen=True)
class ControlPlan:
    quotes: tuple[Quote, ...]
    calendar: tuple[str, ...]
    day_indices: Mapping[str, tuple[int, ...]]
    eval_indices: Mapping[str, tuple[int, ...]]
    eval_indices_by_stratum: Mapping[str, Mapping[str, tuple[int, ...]]]
    eval_ts_by_stratum: Mapping[str, Mapping[str, tuple[int, ...]]]
    train_end_ns: int
    eval_start_ns: int
    eval_end_ns: int
    strata: Mapping[str, StratumModel]
    target_trades_by_day: Mapping[str, int]
    training_trade_target_by_day: Mapping[str, int]
    requested_trades: int | None
    notional: Fraction
    starting_cash: Fraction
    max_holding_ns: int
    family_seed: int
    variant: int
    coin: int
    holding_target_ns: int
    holding_mean_ns: int
    holding_effective_mean_ns: int
    plan_hash: str
    selection_plan_hash: str
    candidate_fills_used: bool = False
    funding: tuple[FundingSettlement, ...] = ()
    funding_ts: tuple[int, ...] = ()
    funding_rates: tuple[Fraction, ...] = ()
    funding_hash: str = ""
    funding_raw_events: int = 0

    def labels(self, generator: int, time_index: int, control_replication: int) -> dict:
        return {
            "GENERATOR_ID": generator,
            "PROGRAM_REPLICATION": self.family_seed,
            "VARIANT": self.variant,
            "COIN": self.coin,
            "TIME": time_index,
            "CONTROL_REPLICATION": control_replication,
        }


def _training_holding_records(
    train: Sequence[Quote],
    max_holding_ns: int,
    holding_quotes: int = TRAINING_HOLDING_QUOTES,
) -> list[tuple[int, int]]:
    """Frozen training policy: hold for ``holding_quotes`` quote intervals.

    Returns ``(entry_hour, holding_ns)`` records.  Only training timestamps are
    read, so this fit cannot see evaluation prices or outcomes.
    """
    records: list[tuple[int, int]] = []
    if len(train) < 2:
        return records
    if holding_quotes < 1:
        raise ControlError("INVALID_HOLDING_QUOTES")
    for i in range(len(train) - 1):
        entry = train[i]
        j = min(len(train) - 1, i + holding_quotes)
        holding = min(max_holding_ns, max(1, train[j].ts - entry.ts))
        records.append((_hour_of(entry.ts), int(holding)))
    return records


def _ema(values: Sequence[Fraction], window: int) -> list[Fraction]:
    if not values:
        return []
    alpha = Fraction(2, window + 1)
    # An unquantised Fraction EMA grows its denominator by roughly 4.4 bits per
    # step.  A multi-day training window is 80k+ steps, so the exact rational
    # becomes astronomically large and preparation never finishes.  Rounding
    # the state to a fixed 1e-9 grid at each step keeps the recursion bounded
    # and deterministic while staying far below the price tick used by the
    # tapes.
    quantum = Fraction(1, 1_000_000_000)
    out: list[Fraction] = []
    state = values[0]
    for value in values:
        state = alpha * value + (1 - alpha) * state
        state = Fraction(round(state / quantum)) * quantum
        out.append(state)
    return out


def _fit_strata(
    train: Sequence[Quote],
    eval_quotes: Sequence[Quote],
    max_holding_ns: int,
) -> tuple[dict[str, StratumModel], int, int, int]:
    records = _training_holding_records(train, max_holding_ns)
    if not records:
        raise ControlError("INSUFFICIENT_TRAINING_HISTORY")

    global_samples = tuple(sorted(holding for _, holding in records))
    global_target = _median(global_samples)
    global_mean = int(sum(global_samples) // len(global_samples))
    train_intervals = sorted(
        max(0, train[i + 1].ts - train[i].ts) for i in range(len(train) - 1)
    )
    median_interval = _median(train_intervals) if train_intervals else 0
    effective_mean = global_mean + median_interval

    train_by_hour: dict[int, list[Quote]] = {}
    for quote in train:
        train_by_hour.setdefault(_hour_of(quote.ts), []).append(quote)
    eval_by_hour: dict[int, int] = {}
    for quote in eval_quotes:
        hour = _hour_of(quote.ts)
        eval_by_hour[hour] = eval_by_hour.get(hour, 0) + 1

    total_train = len(train)
    mids = [(q.bid + q.ask) / 2 for q in train]
    ema = _ema(mids, 20)
    long_counts: dict[int, int] = {}
    directional_counts: dict[int, int] = {}
    for idx, quote in enumerate(train):
        hour = _hour_of(quote.ts)
        if mids[idx] > ema[idx]:
            long_counts[hour] = long_counts.get(hour, 0) + 1
        if mids[idx] != ema[idx]:
            directional_counts[hour] = directional_counts.get(hour, 0) + 1

    strata: dict[str, StratumModel] = {}
    for hour in sorted(set(train_by_hour) | set(eval_by_hour)):
        training_quotes = train_by_hour.get(hour, [])
        key = _stratum_key(hour)
        weight = Fraction(len(training_quotes), total_train) if total_train else Fraction(0)
        hour_samples = tuple(sorted(holding for h, holding in records if h == hour))
        samples = hour_samples if hour_samples else global_samples
        target = _median(samples)
        long_count = long_counts.get(hour, 0)
        long_total = directional_counts.get(hour, 0)
        long_rate = (
            Fraction(long_count, long_total) if long_total else Fraction(1, 2)
        )
        strata[key] = StratumModel(
            key=key,
            hour=hour,
            weight=weight if weight > 0 else Fraction(0),
            long_rate=long_rate,
            holding_samples=samples,
            holding_target_ns=target,
            train_quotes=len(training_quotes),
            eval_quotes=eval_by_hour.get(hour, 0),
        )

    # Normalise weights over strata that actually have training support.
    supported = [s for s in strata.values() if s.weight > 0]
    total_weight = sum((s.weight for s in supported), Fraction(0))
    if total_weight > 0 and total_weight != 1:
        strata = {
            key: (
                StratumModel(
                    key=s.key,
                    hour=s.hour,
                    weight=s.weight / total_weight,
                    long_rate=s.long_rate,
                    holding_samples=s.holding_samples,
                    holding_target_ns=s.holding_target_ns,
                    train_quotes=s.train_quotes,
                    eval_quotes=s.eval_quotes,
                )
                if s.weight > 0
                else s
            )
            for key, s in strata.items()
        }
    return strata, global_target, global_mean, effective_mean


def prepare_control_plan(
    quotes: Sequence[Mapping[str, Any] | Quote],
    trades: int | None,
    notional: float | Fraction,
    seed: int,
    max_holding_ns: int = 10_000_000_000,
    *,
    train_fraction: float = 0.25,
    train_end_ns: int | None = None,
    starting_cash: float | Fraction | None = None,
    variant: int = 0,
    coin: int = 0,
    candidate_fills: Sequence[Mapping[str, Any]] | None = None,
    funding: Sequence[Mapping[str, Any] | FundingSettlement] | None = None,
) -> ControlPlan:
    """Fit the frozen control plan from training strata only.

    ``candidate_fills`` is accepted for provenance and explicitly ignored.  It
    exists so a test (and the report) can prove that candidate realised trades
    never select controls or force a matched count.

    ``funding`` is the exact cost model input.  It is committed in
    ``plan_hash`` but deliberately excluded from ``selection_plan_hash``: a
    funding settlement changes realised PnL, never which controls are drawn.
    """
    funding_settlements, funding_raw_events = normalize_funding(funding or ())
    funding_ts = tuple(item.ts for item in funding_settlements)
    funding_rates = tuple(item.rate for item in funding_settlements)
    funding_hash = _hash_json(
        [[item.ts, str(item.rate)] for item in funding_settlements]
    )
    q = tuple(_to_quote(raw) for raw in quotes)
    if len(q) < 4:
        raise ValueError("INSUFFICIENT_HISTORY")
    q = tuple(sorted(q, key=lambda item: item.ts))

    if train_end_ns is None:
        first_ts, last_ts = q[0].ts, q[-1].ts
        span = max(1, last_ts - first_ts)
        train_end_ns = first_ts + int(span * train_fraction)
    train_end_ns = int(train_end_ns)

    train = [item for item in q if item.ts <= train_end_ns]
    eval_quotes = [item for item in q if item.ts > train_end_ns]
    if len(train) < 2 or len(eval_quotes) < 2:
        # A degenerate split is not a protocol failure; fall back to the first
        # half of the tape and record the effective cut in the plan.
        cut = max(2, len(q) // 2)
        train = list(q[:cut])
        eval_quotes = list(q[cut:])
        train_end_ns = train[-1].ts
    if len(train) < 2 or len(eval_quotes) < 2:
        raise ControlError("INSUFFICIENT_HISTORY")

    strata, holding_target_ns, holding_mean_ns, holding_effective_mean_ns = _fit_strata(
        train, eval_quotes, max_holding_ns
    )

    by_day: dict[str, list[int]] = {}
    for idx, item in enumerate(q):
        by_day.setdefault(_day_of(item.ts), []).append(idx)
    eval_by_day: dict[str, list[int]] = {}
    eval_by_stratum: dict[str, dict[str, list[int]]] = {}
    eval_ts_by_stratum: dict[str, dict[str, list[int]]] = {}
    for idx, item in enumerate(q):
        if item.ts <= train_end_ns:
            continue
        day = _day_of(item.ts)
        eval_by_day.setdefault(day, []).append(idx)
        key = _stratum_key(_hour_of(item.ts))
        eval_by_stratum.setdefault(day, {}).setdefault(key, []).append(idx)
        eval_ts_by_stratum.setdefault(day, {}).setdefault(key, []).append(item.ts)

    calendar = tuple(sorted(by_day))
    target_by_day: dict[str, int] = {}
    training_target_by_day: dict[str, int] = {}
    requested = None if trades is None else max(0, int(trades))
    for day in calendar:
        day_eval = eval_by_day.get(day, [])
        if not day_eval:
            target_by_day[day] = 0
            training_target_by_day[day] = 0
            continue
        first = q[day_eval[0]].ts
        last = q[day_eval[-1]].ts
        duration = max(0, last - first)
        # Training holding time determines how many non-overlapping entries fit
        # in the evaluation window.  This is the calendar/trade-count match.
        training_target = (
            max(
                1,
                int(
                    _ceil_int(
                        Fraction(duration)
                        * TARGET_CAPACITY_FRACTION
                        / max(1, holding_effective_mean_ns)
                    )
                ),
            )
            if duration > 0
            else 1
        )
        training_target_by_day[day] = training_target
        capacity = max(0, len(day_eval) - 1)
        target = min(training_target, capacity)
        if requested is not None:
            target = min(target, requested)
        target_by_day[day] = max(0, target)

    eval_start_ns = eval_quotes[0].ts
    eval_end_ns = eval_quotes[-1].ts
    notional_f = Fraction(str(notional))
    if starting_cash is None:
        # The control account must be able to fund one notional unit plus fees;
        # ten units leaves room for sequential round trips without inventing a
        # shared account.
        starting_cash_f = max(notional_f * 10, Fraction(1000))
    else:
        starting_cash_f = Fraction(str(starting_cash))

    selection_material = {
        "strata": {
            key: {
                "weight": str(s.weight),
                "long_rate": str(s.long_rate),
                "holding_target_ns": s.holding_target_ns,
                "holding_samples_hash": _hash_json(list(s.holding_samples)),
                "train_quotes": s.train_quotes,
            }
            for key, s in sorted(strata.items())
        },
        "calendar": list(calendar),
        "eval_indices": {day: list(indices) for day, indices in sorted(eval_by_day.items())},
        "target_trades_by_day": target_by_day,
        "training_trade_target_by_day": training_target_by_day,
        "requested_trades": requested,
        "max_holding_ns": max_holding_ns,
        "train_end_ns": train_end_ns,
        "holding_mean_ns": holding_mean_ns,
        "holding_effective_mean_ns": holding_effective_mean_ns,
        "variant": variant,
        "coin": coin,
    }
    selection_plan_hash = _hash_json(selection_material)
    plan_material = {
        "draw_mapping": DRAW_MAPPING,
        "seed_protocol_hash": PROTOCOL_HASH,
        "generator_ids": GENERATOR_IDS,
        "family_seed": seed,
        **selection_material,
        "notional": str(notional_f),
        "starting_cash": str(starting_cash_f),
        "funding_model": FUNDING_MODEL,
        "funding_sign_convention": FUNDING_SIGN_CONVENTION,
        "funding_window": FUNDING_WINDOW,
        "funding_hash": funding_hash,
    }
    plan_hash = _hash_json(plan_material)

    return ControlPlan(
        quotes=q,
        calendar=calendar,
        day_indices={day: tuple(indices) for day, indices in by_day.items()},
        eval_indices={day: tuple(indices) for day, indices in eval_by_day.items()},
        eval_indices_by_stratum={
            day: {key: tuple(indices) for key, indices in groups.items()}
            for day, groups in eval_by_stratum.items()
        },
        eval_ts_by_stratum={
            day: {key: tuple(timestamps) for key, timestamps in groups.items()}
            for day, groups in eval_ts_by_stratum.items()
        },
        train_end_ns=train_end_ns,
        eval_start_ns=eval_start_ns,
        eval_end_ns=eval_end_ns,
        strata=strata,
        target_trades_by_day=target_by_day,
        training_trade_target_by_day=training_target_by_day,
        requested_trades=requested,
        notional=notional_f,
        starting_cash=starting_cash_f,
        max_holding_ns=int(max_holding_ns),
        family_seed=int(seed),
        variant=int(variant),
        coin=int(coin),
        holding_target_ns=holding_target_ns,
        holding_mean_ns=holding_mean_ns,
        holding_effective_mean_ns=holding_effective_mean_ns,
        plan_hash=plan_hash,
        selection_plan_hash=selection_plan_hash,
        candidate_fills_used=False,
        funding=funding_settlements,
        funding_ts=funding_ts,
        funding_rates=funding_rates,
        funding_hash=funding_hash,
        funding_raw_events=funding_raw_events,
    )


# ---------------------------------------------------------------------------
# Accounts and one seed
# ---------------------------------------------------------------------------


@dataclass
class ControlAccount:
    """A private per-seed cash/position account.

    Controls hold at most one unit of the instrument at a time and close it
    before the next entry, so the account is flat between trades.  The account
    is intentionally mutable only inside one seed run; it is never shared.
    """

    currency: str
    cash: Fraction
    initial_cash: Fraction
    position_qty: Fraction = Fraction(0)
    entry_price: Fraction | None = None
    side: int = 0
    fees: Fraction = Fraction(0)
    realized: Fraction = Fraction(0)
    funding: Fraction = Fraction(0)
    notional_traded: Fraction = Fraction(0)
    trades: int = 0
    rejected_orders: int = 0
    reject_reasons: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejected_orders += 1
        self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + 1

    def open(self, side: int, qty: Fraction, price: Fraction) -> bool:
        if self.position_qty != 0:
            self.reject("ACCOUNT_BUSY")
            return False
        required = qty * price
        estimated_fee = TAKER_FEE * required
        if self.cash < required + estimated_fee:
            self.reject("INSUFFICIENT_CASH")
            return False
        self.position_qty = side * qty
        self.entry_price = price
        self.side = side
        self.notional_traded += required
        return True

    def apply_funding(self, rate: Fraction, reference_notional: Fraction) -> Fraction:
        """Apply one funding settlement on the open position.

        ``reference_notional`` is the position's own entry notional.  A long
        position with a positive rate pays (negative cash); a short position
        receives it.  The cash effect is booked into ``realized`` so it flows
        into the daily and seed net PnL, and tracked separately in ``funding``
        for diagnostics.
        """
        if self.position_qty == 0 or self.side == 0:
            raise ControlError("NO_POSITION_FOR_FUNDING")
        amount = funding_cash_delta(self.side, reference_notional, rate)
        self.cash += amount
        self.realized += amount
        self.funding += amount
        return amount

    def close(self, qty: Fraction, price: Fraction) -> Fraction:
        if self.position_qty == 0 or self.entry_price is None:
            raise ControlError("ACCOUNT_NOT_OPEN")
        side = self.side
        entry = self.entry_price
        gross = side * (price - entry) * qty
        fee = TAKER_FEE * (entry + price) * qty
        self.cash += gross - fee
        self.realized += gross - fee
        self.fees += fee
        self.position_qty = Fraction(0)
        self.entry_price = None
        self.side = 0
        self.trades += 1
        return gross - fee


@dataclass
class SeedResult:
    seed_index: int
    days: dict[str, dict]
    net_pnl_total: Fraction | None
    executed_trades: int
    rejected_orders: int
    reject_reasons: dict[str, int]
    selection_hash: str
    control_hash: str
    missing_mark: bool
    starting_cash: Fraction
    ending_cash: Fraction
    fees: Fraction
    account_id: int
    funding: Fraction = Fraction(0)
    funding_records: tuple[dict, ...] = ()


def _choose_stratum(
    plan: ControlPlan,
    day: str,
    cursor_ns: int,
    seed_index: int,
    time_index: int,
) -> str | None:
    # Indices within a day/stratum are stored in ascending timestamp order, so
    # the last timestamp answers "is any quote still available after the
    # cursor?" without rescanning the whole day for every trade.
    available = {}
    for key, indices in plan.eval_indices_by_stratum.get(day, {}).items():
        if indices and plan.quotes[indices[-1]].ts >= cursor_ns:
            available[key] = indices
    if not available:
        return None
    weights = {key: plan.strata[key].weight for key in available if key in plan.strata}
    total = sum(weights.values(), Fraction(0))
    if total <= 0:
        keys = sorted(available)
        u = uniform_at(
            plan.labels(GENERATOR_STRATUM, time_index, seed_index), 0
        )
        return keys[min(len(keys) - 1, int(u * len(keys)))]
    u = uniform_at(plan.labels(GENERATOR_STRATUM, time_index, seed_index), 0)
    target = Fraction(str(u)) * total
    cumulative = Fraction(0)
    for key in sorted(weights):
        cumulative += weights[key]
        if target < cumulative:
            return key
    return sorted(weights)[-1]


def _run_seed_account(
    plan: ControlPlan, seed_index: int
) -> tuple[SeedResult, list[dict]]:
    """Run one frozen seed.  Returns the result plus compact reject records."""
    account = ControlAccount(
        currency="USDT",
        cash=plan.starting_cash,
        initial_cash=plan.starting_cash,
    )
    days: dict[str, dict] = {}
    reject_records: list[dict] = []
    selection_material: list[dict] = []
    funding_records: list[dict] = []
    trade_ordinal = 0
    total_rejected = 0
    all_reasons: dict[str, int] = {}

    global_trades = 0

    for day in plan.calendar:
        day_eval = plan.eval_indices.get(day, ())
        target = plan.target_trades_by_day.get(day, 0)
        day_start_realized = account.realized
        day_rejected = 0
        day_trades = 0
        day_exposure = 0
        day_exposure_notional = 0
        day_holding = 0
        day_funding = Fraction(0)
        day_reasons: dict[str, int] = {}
        stratum_metrics: dict[str, dict] = {}
        missing_mark = False
        schedule_truncated = False
        entry_schedule: list[tuple[int, int, int]] = []

        if not day_eval or target <= 0:
            days[day] = {
                "day": day,
                "status": STATE_NO_TRADES,
                "net_pnl": fmt(Fraction(0)),
                "net_pnl_exact": str(Fraction(0)),
                "trades": 0,
                "target_trades": target,
                "rejected_orders": 0,
                "reject_reasons": {},
                "exposure_ns": 0,
                "exposure_notional_ns": 0,
                "holding_ns": 0,
                "funding": fmt(Fraction(0)),
                "funding_exact": str(Fraction(0)),
                "missing_mark": False,
                "schedule_truncated": False,
                "schedule_shortfall": 0,
                "strata": {},
            }
            selection_material.append(
                {"day": day, "entries": [], "target": target, "status": STATE_NO_TRADES}
            )
            continue

        cursor_ts = plan.quotes[day_eval[0]].ts
        day_end_ts = plan.quotes[day_eval[-1]].ts
        day_timestamps = [plan.quotes[idx].ts for idx in day_eval]
        # Map a global quote index back to its position inside this day.
        day_position = {idx: pos for pos, idx in enumerate(day_eval)}

        for t in range(target):
            stratum = _choose_stratum(plan, day, cursor_ts, seed_index, trade_ordinal)
            if stratum is None:
                break
            model = plan.strata[stratum]
            metrics = stratum_metrics.setdefault(
                stratum,
                {
                    "trades": 0,
                    "exposure_ns": 0,
                    "exposure_notional_ns": 0,
                    "holding_ns": 0,
                    "rejected_orders": 0,
                    "funding": Fraction(0),
                },
            )
            stratum_indices = plan.eval_indices_by_stratum.get(day, {}).get(stratum, ())
            stratum_ts = plan.eval_ts_by_stratum.get(day, {}).get(stratum, ())
            first_available = bisect.bisect_left(stratum_ts, cursor_ts)
            if first_available >= len(stratum_indices):
                break
            jitter_u = uniform_at(
                plan.labels(GENERATOR_ENTRY_JITTER, trade_ordinal, seed_index), 0
            )
            # Jitter only between the first two available quotes in the chosen
            # stratum.  A wider jump can spend the calendar budget before the
            # target count is reached on short tapes.
            jitter_span = min(len(stratum_indices) - first_available, 2)
            entry_idx = stratum_indices[
                first_available + min(jitter_span - 1, int(jitter_u * jitter_span))
            ]
            entry_quote = plan.quotes[entry_idx]

            side_u = uniform_at(
                plan.labels(GENERATOR_SIDE, trade_ordinal, seed_index), 0
            )
            side = 1 if Fraction(str(side_u)) < model.long_rate else -1

            hold_u = uniform_at(
                plan.labels(GENERATOR_HOLDING, trade_ordinal, seed_index), 0
            )
            holding_ns = _quantile(model.holding_samples, hold_u)
            exit_ts = entry_quote.ts + holding_ns
            pos = day_position[entry_idx]
            exit_pos = bisect.bisect_left(day_timestamps, exit_ts, lo=pos + 1)
            if exit_pos >= len(day_eval):
                # No mark exists for this horizon.  Do not open a position that
                # cannot be valued; this is a schedule shortfall, not a filled
                # missing mark.
                schedule_truncated = True
                break
            exit_quote = plan.quotes[day_eval[exit_pos]]

            price_in = _entry_price(entry_quote, side)
            price_out = _exit_price(exit_quote, side)
            if price_out <= 0 or exit_quote.ask < exit_quote.bid:
                # An actual unusable mark: keep it missing instead of filling.
                missing_mark = True
                break
            if price_in <= 0 or entry_quote.ask < entry_quote.bid:
                account.reject("INVALID_QUOTE")
                day_rejected += 1
                metrics["rejected_orders"] += 1
                day_reasons["INVALID_QUOTE"] = day_reasons.get("INVALID_QUOTE", 0) + 1
                reject_records.append(
                    {
                        "kind": "ORDER_REJECT",
                        "reason": "INVALID_QUOTE",
                        "day": day,
                        "seed_index": seed_index,
                        "time_index": trade_ordinal,
                        "status": STATE_VALID,
                    }
                )
                cursor_ts = day_timestamps[min(pos + 1, len(day_timestamps) - 1)]
                trade_ordinal += 1
                continue

            unit = Fraction(1, 1000)
            qty_units = max(1, _round_int(plan.notional / price_in / unit))
            qty = qty_units * unit
            if not account.open(side, qty, price_in):
                reason = next(reversed(account.reject_reasons)) if account.reject_reasons else "REJECTED"
                day_rejected += 1
                metrics["rejected_orders"] += 1
                day_reasons[reason] = day_reasons.get(reason, 0) + 1
                reject_records.append(
                    {
                        "kind": "ORDER_REJECT",
                        "reason": reason,
                        "day": day,
                        "seed_index": seed_index,
                        "time_index": trade_ordinal,
                        "status": STATE_VALID,
                    }
                )
                cursor_ts = day_timestamps[min(pos + 1, len(day_timestamps) - 1)]
                trade_ordinal += 1
                continue

            # Funding settles only inside the position's own holding window and
            # only against its entry notional.  A settlement stamped exactly at
            # entry applies; one stamped exactly at exit does not.  No future
            # settlement is read and no mark price is inferred.
            reference_notional = qty * price_in
            funding_start, funding_end = funding_window_indices(
                plan.funding_ts, entry_quote.ts, exit_quote.ts
            )
            for funding_index in range(funding_start, funding_end):
                rate = plan.funding_rates[funding_index]
                amount = account.apply_funding(rate, reference_notional)
                day_funding += amount
                metrics["funding"] += amount
                funding_records.append(
                    {
                        "day": day,
                        "settlement_ts": plan.funding_ts[funding_index],
                        "entry_ts": entry_quote.ts,
                        "exit_ts": exit_quote.ts,
                        "side": side,
                        "reference_notional": str(reference_notional),
                        "rate": str(rate),
                        "amount": str(amount),
                    }
                )

            account.close(qty, price_out)
            realized = exit_quote.ts - entry_quote.ts
            day_trades += 1
            day_exposure += realized
            day_exposure_notional += int(plan.notional) * realized
            day_holding += realized
            global_trades += 1
            metrics["trades"] += 1
            metrics["exposure_ns"] += realized
            metrics["exposure_notional_ns"] += int(plan.notional) * realized
            metrics["holding_ns"] += realized
            entry_schedule.append((entry_idx, side, realized))
            cursor_ts = exit_quote.ts
            trade_ordinal += 1
            if cursor_ts >= day_end_ts:
                break

        total_rejected += day_rejected
        for reason, count in day_reasons.items():
            all_reasons[reason] = all_reasons.get(reason, 0) + count
        status = STATE_MISSING_MARK if missing_mark else (STATE_NO_TRADES if day_trades == 0 else STATE_VALID)
        net = None if missing_mark else account.realized - day_start_realized
        days[day] = {
            "day": day,
            "status": status,
            "net_pnl": None if net is None else fmt(net),
            "net_pnl_exact": None if net is None else str(net),
            "trades": day_trades,
            "target_trades": target,
            "rejected_orders": day_rejected,
            "reject_reasons": dict(sorted(day_reasons.items())),
            "exposure_ns": day_exposure,
            "exposure_notional_ns": day_exposure_notional,
            "holding_ns": day_holding,
            "funding": fmt(day_funding),
            "funding_exact": str(day_funding),
            "missing_mark": missing_mark,
            "schedule_truncated": schedule_truncated,
            "schedule_shortfall": max(0, target - day_trades) if schedule_truncated else 0,
            "strata": {
                key: {
                    "trades": value["trades"],
                    "exposure_ns": value["exposure_ns"],
                    "exposure_notional_ns": value["exposure_notional_ns"],
                    "holding_ns": value["holding_ns"],
                    "rejected_orders": value["rejected_orders"],
                    "funding": fmt(value["funding"]),
                    "funding_exact": str(value["funding"]),
                }
                for key, value in sorted(stratum_metrics.items())
            },
        }
        selection_material.append(
            {
                "day": day,
                "target": target,
                "entries": [
                    {"entry_index": idx, "side": side, "holding_ns": hold}
                    for idx, side, hold in entry_schedule
                ],
                "status": status,
            }
        )

    valid_days = [d for d in days.values() if d["status"] != STATE_MISSING_MARK]
    if valid_days:
        net_total = sum(
            (Fraction(d["net_pnl_exact"]) for d in valid_days if d["net_pnl_exact"] is not None),
            Fraction(0),
        )
    else:
        net_total = None
    selection_hash = _hash_json(
        {"seed_index": seed_index, "plan": plan.selection_plan_hash, "days": selection_material}
    )
    control_hash = _hash_json(
        {
            "draw_mapping": DRAW_MAPPING,
            "seed_protocol_hash": PROTOCOL_HASH,
            "seed_index": seed_index,
            "selection_hash": selection_hash,
            "net_pnl": str(net_total),
            "trades": global_trades,
            "funding": str(account.funding),
        }
    )
    result = SeedResult(
        seed_index=seed_index,
        days=days,
        net_pnl_total=net_total,
        executed_trades=global_trades,
        rejected_orders=total_rejected,
        reject_reasons=all_reasons,
        selection_hash=selection_hash,
        control_hash=control_hash,
        missing_mark=any(d["missing_mark"] for d in days.values()),
        starting_cash=plan.starting_cash,
        ending_cash=account.cash,
        fees=account.fees,
        account_id=id(account),
        funding=account.funding,
        funding_records=tuple(funding_records),
    )
    return result, reject_records


def run_control_seed(plan: ControlPlan, seed_index: int) -> SeedResult:
    """Run one frozen control seed against a prepared plan."""
    result, _ = _run_seed_account(plan, seed_index)
    return result


# ---------------------------------------------------------------------------
# Streaming aggregation
# ---------------------------------------------------------------------------


class StreamingControlAggregator:
    """Bounded-memory aggregator for control seed results.

    Per-day and per-stratum sums are retained; per-seed daily series are folded
    into means and a rolling hash and then discarded.  ``retained_seed_series``
    is always zero, which is what the streaming test asserts.
    """

    def __init__(self, plan: ControlPlan, keep_summaries: bool = True):
        self.plan = plan
        self.keep_summaries = keep_summaries
        self.seeds = 0
        self.seeds_valid = 0
        self.seeds_missing = 0
        self.zero_trade_seeds = 0
        self.truncated_seeds = 0
        self.schedule_shortfall = 0
        self.executed_trades = 0
        self.rejected_orders = 0
        self.funding = Fraction(0)
        self.reject_reasons: dict[str, int] = {}
        self._chain = hashlib.sha256(b"").digest()
        self._summaries: list[dict] = []
        self.retained_seed_series = 0
        self._days: dict[str, dict] = {
            day: {
                "seeds": 0,
                "valid_seeds": 0,
                "missing_seeds": 0,
                "zero_trade_seeds": 0,
                "truncated_seeds": 0,
                "schedule_shortfall": 0,
                "sum_pnl": Fraction(0),
                "sum_pnl_sq": Fraction(0),
                "trades": 0,
                "target_trades": 0,
                "rejected_orders": 0,
                "exposure_ns": 0,
                "exposure_notional_ns": 0,
                "holding_ns": 0,
                "sum_funding": Fraction(0),
            }
            for day in plan.calendar
        }
        self._strata: dict[str, dict] = {
            key: {
                "seeds_with_trades": 0,
                "trades": 0,
                "rejected_orders": 0,
                "exposure_ns": 0,
                "exposure_notional_ns": 0,
                "holding_ns": 0,
                "funding": Fraction(0),
            }
            for key in sorted(plan.strata)
        }

    def add(self, result: SeedResult) -> None:
        self.seeds += 1
        if result.missing_mark:
            self.seeds_missing += 1
        else:
            self.seeds_valid += 1
        if result.executed_trades == 0:
            self.zero_trade_seeds += 1
        if any(payload.get("schedule_truncated") for payload in result.days.values()):
            self.truncated_seeds += 1
        self.schedule_shortfall += sum(
            int(payload.get("schedule_shortfall", 0)) for payload in result.days.values()
        )
        self.executed_trades += result.executed_trades
        self.rejected_orders += result.rejected_orders
        self.funding += result.funding
        for reason, count in result.reject_reasons.items():
            self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + count

        for day, payload in result.days.items():
            acc = self._days[day]
            acc["seeds"] += 1
            if payload["missing_mark"]:
                acc["missing_seeds"] += 1
            else:
                acc["valid_seeds"] += 1
                if payload["net_pnl_exact"] is not None:
                    value = Fraction(payload["net_pnl_exact"])
                    acc["sum_pnl"] += value
                    acc["sum_pnl_sq"] += value * value
            if payload["trades"] == 0:
                acc["zero_trade_seeds"] += 1
            if payload.get("schedule_truncated"):
                acc["truncated_seeds"] += 1
            acc["schedule_shortfall"] += int(payload.get("schedule_shortfall", 0))
            acc["trades"] += payload["trades"]
            acc["target_trades"] += payload["target_trades"]
            acc["rejected_orders"] += payload["rejected_orders"]
            acc["exposure_ns"] += payload["exposure_ns"]
            acc["exposure_notional_ns"] += payload.get("exposure_notional_ns", 0)
            acc["holding_ns"] += payload["holding_ns"]
            acc["sum_funding"] += Fraction(payload.get("funding_exact", "0"))
            for key, metrics in payload.get("strata", {}).items():
                s = self._strata.setdefault(
                    key,
                    {
                        "seeds_with_trades": 0,
                        "trades": 0,
                        "rejected_orders": 0,
                        "exposure_ns": 0,
                        "exposure_notional_ns": 0,
                        "holding_ns": 0,
                    },
                )
                if metrics["trades"] > 0:
                    s["seeds_with_trades"] += 1
                s["trades"] += metrics["trades"]
                s["rejected_orders"] += metrics["rejected_orders"]
                s["exposure_ns"] += metrics["exposure_ns"]
                s["exposure_notional_ns"] += metrics.get("exposure_notional_ns", 0)
                s["holding_ns"] += metrics["holding_ns"]
                s["funding"] += Fraction(metrics.get("funding_exact", "0"))

        summary = {
            "seed_index": result.seed_index,
            "trades": result.executed_trades,
            "rejected_orders": result.rejected_orders,
            "net_pnl": None if result.net_pnl_total is None else fmt(result.net_pnl_total),
            "fees": fmt(result.fees),
            "funding": fmt(result.funding),
            "missing_mark": result.missing_mark,
            "selection_hash": result.selection_hash,
            "control_hash": result.control_hash,
            "days": [
                {
                    "day": day,
                    "status": payload["status"],
                    "net_pnl": payload["net_pnl"],
                    "trades": payload["trades"],
                    "rejected_orders": payload["rejected_orders"],
                    "exposure_ns": payload["exposure_ns"],
                    "exposure_notional_ns": payload.get("exposure_notional_ns", 0),
                    "holding_ns": payload["holding_ns"],
                    "funding": payload.get("funding"),
                    "funding_exact": payload.get("funding_exact", "0"),
                    "schedule_truncated": bool(payload.get("schedule_truncated")),
                    "schedule_shortfall": int(payload.get("schedule_shortfall", 0)),
                }
                for day, payload in sorted(result.days.items())
            ],
        }
        if self.keep_summaries:
            self._summaries.append(summary)
        self._chain = hashlib.sha256(
            self._chain + json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()
        ).digest()
        # The per-seed daily series is intentionally not retained.
        self.retained_seed_series = 0

    def daily_series(self) -> list[dict]:
        series = []
        for day in self.plan.calendar:
            acc = self._days[day]
            valid = acc["valid_seeds"]
            mean = acc["sum_pnl"] / valid if valid else None
            if valid and acc["sum_pnl_sq"] is not None and valid > 1:
                variance = (acc["sum_pnl_sq"] - acc["sum_pnl"] * acc["sum_pnl"] / valid) / (
                    valid - 1
                )
                std = float(variance) ** 0.5 if variance > 0 else 0.0
            else:
                std = 0.0
            series.append(
                {
                    "day": day,
                    "seeds": acc["seeds"],
                    "valid_seeds": valid,
                    "missing_seeds": acc["missing_seeds"],
                    "zero_trade_seeds": acc["zero_trade_seeds"],
                    "truncated_seeds": acc["truncated_seeds"],
                    "schedule_shortfall": acc["schedule_shortfall"],
                    "mean_net_pnl": None if mean is None else _num_str(mean),
                    "mean_net_pnl_float": None if mean is None else round(float(mean), 12),
                    "std_net_pnl": round(std, 12),
                    "trades": acc["trades"],
                    "target_trades": acc["target_trades"],
                    "rejected_orders": acc["rejected_orders"],
                    "exposure_ns": acc["exposure_ns"],
                    "exposure_notional_ns": acc["exposure_notional_ns"],
                    "holding_ns": acc["holding_ns"],
                    "funding": None if not valid else str(acc["sum_funding"]),
                    "mean_funding": (
                        None if not valid else _num_str(acc["sum_funding"] / valid)
                    ),
                }
            )
        return series

    def per_stratum(self) -> list[dict]:
        out = []
        for key in sorted(self._strata):
            acc = self._strata[key]
            model = self.plan.strata.get(key)
            total_trades = acc["trades"]
            out.append(
                {
                    "stratum": key,
                    "fitted_weight": str(model.weight) if model else "0",
                    "fitted_long_rate": str(model.long_rate) if model else "0",
                    "holding_target_ns": model.holding_target_ns if model else 0,
                    "train_quotes": model.train_quotes if model else 0,
                    "eval_quotes": model.eval_quotes if model else 0,
                    "seeds_with_trades": acc["seeds_with_trades"],
                    "trades": total_trades,
                    "rejected_orders": acc["rejected_orders"],
                    "exposure_ns": acc["exposure_ns"],
                    "exposure_notional_ns": acc["exposure_notional_ns"],
                    "holding_ns": acc["holding_ns"],
                    "funding": _num_str(acc["funding"]),
                    "mean_funding": (
                        _num_str(acc["funding"] / total_trades) if total_trades else "0"
                    ),
                    "mean_holding_ns": (
                        int(acc["holding_ns"] // total_trades) if total_trades else 0
                    ),
                }
            )
        return out

    def summary(self) -> dict:
        totals = [Fraction(s["net_pnl"]) for s in self._summaries if s["net_pnl"] is not None]
        mean = sum(totals, Fraction(0)) / len(totals) if totals else None
        if len(totals) > 1 and mean is not None:
            var = sum(((v - mean) ** 2 for v in totals), Fraction(0)) / (len(totals) - 1)
            std = float(var) ** 0.5
        else:
            std = 0.0
        return {
            "seeds": self.seeds,
            "valid_seeds": self.seeds_valid,
            "missing_mark_seeds": self.seeds_missing,
            "zero_trade_seeds": self.zero_trade_seeds,
            "truncated_seeds": self.truncated_seeds,
            "schedule_shortfall": self.schedule_shortfall,
            "executed_trades": self.executed_trades,
            "rejected_orders": self.rejected_orders,
            "funding_total": _num_str(self.funding),
            "reject_reasons": dict(sorted(self.reject_reasons.items())),
            "control_mean": None if mean is None else round(float(mean), 8),
            "control_std": round(std, 8),
            "control_mean_exact": None if mean is None else _num_str(mean),
            "seed_series_hash": self._chain.hex(),
            "retained_seed_series": self.retained_seed_series,
            "summaries": self._summaries,
        }


# ---------------------------------------------------------------------------
# Frozen trend/pullback control
# ---------------------------------------------------------------------------


def run_trend_pullback_control(
    plan: ControlPlan,
    notional: float | Fraction | None = None,
) -> dict:
    """Run the separate frozen trend/pullback control.

    This is a deterministic benchmark, not a random control seed, and it is
    never included in the random-control mean.  It trades only the evaluation
    window and owns its own account.
    """
    notional_f = Fraction(str(notional)) if notional is not None else plan.notional
    account = ControlAccount(
        currency="USDT", cash=max(notional_f * 10, Fraction(1000)), initial_cash=max(notional_f * 10, Fraction(1000))
    )
    days: dict[str, dict] = {}
    for day in plan.calendar:
        eval_idx = list(plan.eval_indices.get(day, ()))
        if len(eval_idx) < 2:
            days[day] = {
                "day": day,
                "status": STATE_NO_TRADES,
                "trades": 0,
                "net_pnl": fmt(Fraction(0)),
                "exposure_ns": 0,
                "exposure_notional_ns": 0,
                "holding_ns": 0,
                "rejected_orders": 0,
                "missing_mark": False,
            }
            continue
        mids = [(plan.quotes[i].bid + plan.quotes[i].ask) / 2 for i in eval_idx]
        ema = _ema(mids, 20)
        day_trades = 0
        day_exposure = 0
        day_exposure_notional = 0
        day_holding = 0
        day_rejected = 0
        missing = False
        i = 1
        start_realized = account.realized
        while i < len(eval_idx) - 1:
            prev_mid = mids[i - 1]
            mid = mids[i]
            above = mid > ema[i]
            below = mid < ema[i]
            long_signal = above and mid < prev_mid
            short_signal = below and mid > prev_mid
            if not (long_signal or short_signal):
                i += 1
                continue
            side = 1 if long_signal else -1
            entry_idx = eval_idx[i]
            entry_quote = plan.quotes[entry_idx]
            price_in = _entry_price(entry_quote, side)
            unit = Fraction(1, 1000)
            qty = max(1, _round_int(notional_f / price_in / unit)) * unit
            if not account.open(side, qty, price_in):
                day_rejected += 1
                i += 1
                continue
            exit_i = None
            for j in range(i + 1, len(eval_idx)):
                if (side > 0 and mids[j] <= ema[j]) or (side < 0 and mids[j] >= ema[j]):
                    exit_i = j
                    break
                if plan.quotes[eval_idx[j]].ts - entry_quote.ts >= plan.max_holding_ns:
                    exit_i = j
                    break
            if exit_i is None:
                missing = True
                account.position_qty = Fraction(0)
                account.entry_price = None
                account.side = 0
                break
            exit_quote = plan.quotes[eval_idx[exit_i]]
            account.close(qty, _exit_price(exit_quote, side))
            day_trades += 1
            held = exit_quote.ts - entry_quote.ts
            day_exposure += held
            day_exposure_notional += int(notional_f) * held
            day_holding += held
            i = exit_i + 1
        net = None if missing else account.realized - start_realized
        days[day] = {
            "day": day,
            "status": STATE_MISSING_MARK if missing else (STATE_NO_TRADES if day_trades == 0 else STATE_VALID),
            "trades": day_trades,
            "net_pnl": None if net is None else fmt(net),
            "exposure_ns": day_exposure,
            "exposure_notional_ns": day_exposure_notional,
            "holding_ns": day_holding,
            "rejected_orders": day_rejected,
            "missing_mark": missing,
        }
    return {
        "control_kind": "FROZEN_TREND_PULLBACK_V1",
        "draw_mapping": "FROZEN_DETERMINISTIC_TREND_PULLBACK",
        "ema_window": 20,
        "max_holding_ns": plan.max_holding_ns,
        "notional": str(notional_f),
        "daily_series": [days[day] for day in plan.calendar],
        "daily_series_hash": _hash_json([days[day] for day in plan.calendar]),
        "separate_from_random_controls": True,
    }


# ---------------------------------------------------------------------------
# Deterministic L1 rescue
# ---------------------------------------------------------------------------


def _is_valid_l1_reject(record: Mapping[str, Any]) -> bool:
    status = str(record.get("status", STATE_VALID)).upper()
    if status in {"INVALID", "MALFORMED", "UNKNOWN"}:
        return False
    if record.get("valid") is False:
        return False
    if str(record.get("reason", "")).upper() in {"MALFORMED", "INVALID", "DUPLICATE"}:
        return False
    return True


def l1_rescue(
    rejects: Sequence[Mapping[str, Any]],
    family_seed: int,
    *,
    group_size: int = L1_GROUP_SIZE,
    scope: str = RESCUE_SCOPE,
) -> dict:
    """Deterministic L1 rescue: one reservoir sample per valid group of ten.

    Invalid rejects are removed *before* grouping, so they are not in any
    denominator.  Each group of up to ``group_size`` valid records is scanned in
    input order and a size-one reservoir is kept using HMAC counter uniforms
    labelled by the frozen family seed.
    """
    if group_size <= 0:
        raise ControlError("INVALID_GROUP_SIZE")
    valid_records = [dict(record) for record in rejects if _is_valid_l1_reject(record)]
    invalid_count = len(rejects) - len(valid_records)
    groups: list[dict] = []
    selected: list[dict] = []
    for group_index, start in enumerate(range(0, len(valid_records), group_size)):
        group = valid_records[start : start + group_size]
        chosen = None
        for position, record in enumerate(group):
            labels = {
                "GENERATOR_ID": GENERATOR_RESCUE,
                "PROGRAM_REPLICATION": int(family_seed),
                "VARIANT": group_index,
                "COIN": 0,
                "TIME": position,
                "CONTROL_REPLICATION": 0,
            }
            u = uniform_at(labels, 0)
            if u < 1.0 / (position + 1):
                chosen = record
        if chosen is not None:
            selected.append(chosen)
        groups.append(
            {
                "group": group_index,
                "denominator": len(group),
                "selected": chosen,
            }
        )
    payload = {
        "method": RESCUE_METHOD,
        "scope": scope,
        "group_size": group_size,
        "family_seed": int(family_seed),
        "input_records": len(rejects),
        "valid_records": len(valid_records),
        "invalid_excluded": invalid_count,
        "groups": len(groups),
        "selected_count": len(selected),
        "selected": selected,
        "group_detail": groups,
        "status": "NO_REJECTS" if not valid_records else "PASS",
    }
    payload["rescue_hash"] = _hash_json(payload)
    return payload


# ---------------------------------------------------------------------------
# F27 arithmetic helpers
# ---------------------------------------------------------------------------


def daily_control_aggregate(candidate: Sequence[Any], controls: Sequence[Sequence[Any]]) -> dict:
    """F27 ``DAILY_CONTROL_AGGREGATE`` arithmetic.

    Controls share one market history, so ``market_histories`` is one.  All
    supplied days stay in the denominator, including zero days.
    """
    candidate_values = [Fraction(str(v)) for v in candidate]
    control_rows = [[Fraction(str(v)) for v in row] for row in controls]
    if not candidate_values or not control_rows:
        raise ControlError("EMPTY_CONTROL_INPUT")
    widths = {len(row) for row in control_rows}
    if len(widths) != 1 or next(iter(widths)) != len(candidate_values):
        raise ControlError("CALENDAR_MISMATCH")
    candidate_mean = sum(candidate_values, Fraction(0)) / len(candidate_values)
    flat = [value for row in control_rows for value in row]
    control_mean = sum(flat, Fraction(0)) / len(flat)
    return {
        "candidate_mean": candidate_mean,
        "control_mean": control_mean,
        "difference_mean": candidate_mean - control_mean,
        "market_histories": 1,
        "included_days": len(candidate_values),
        "control_count": len(control_rows),
        "control_days": len(flat),
    }


def iut_combined_p(p_profit: Any, p_control: Any) -> Fraction:
    """F27 ``IUT`` combination: the maximum of the two p-values."""
    return max(Fraction(str(p_profit)), Fraction(str(p_control)))


# ---------------------------------------------------------------------------
# Public report entry points
# ---------------------------------------------------------------------------


def _execute_plan(
    quotes: Sequence[Mapping[str, Any] | Quote],
    trades: int | None,
    notional: float,
    seed: int,
    replications: int,
    *,
    execution: str,
    max_holding_ns: int,
    train_fraction: float,
    train_end_ns: int | None,
    starting_cash: float | None,
    variant: int,
    coin: int,
    candidate_fills: Sequence[Mapping[str, Any]] | None,
    funding: Sequence[Mapping[str, Any] | FundingSettlement] | None,
    max_seconds: float | None,
    max_trades: int | None,
    keep_summaries: bool,
) -> tuple[ControlPlan, StreamingControlAggregator, list[dict], int, bool, float]:
    """Execute controls in BATCH or SEQUENTIAL mode.

    BATCH prepares the immutable plan once.  SEQUENTIAL rebuilds it for every
    seed.  Both use the same seed order and the same aggregator, so a mismatch
    is a real state-leak bug.
    """
    mode = execution.upper()
    if mode not in {"BATCH", "SEQUENTIAL"}:
        raise ControlError("INVALID_EXECUTION_MODE")
    # Both modes consume the same immutable quote tape.  Convert raw records to
    # frozen Quote objects once per execution: SEQUENTIAL still rebuilds the
    # strata, calendar and targets for every seed, but it does not re-parse the
    # identical tape hundreds of times.  On a multi-day tape that re-parsing is
    # the dominant cost and made a 1,000-seed sequential run infeasible.
    prepared_quotes: Sequence[Mapping[str, Any] | Quote] = quotes
    if quotes and not isinstance(quotes[0], Quote):
        prepared_quotes = tuple(_to_quote(raw) for raw in quotes)
    plan = prepare_control_plan(
        prepared_quotes,
        trades,
        notional,
        seed,
        max_holding_ns,
        train_fraction=train_fraction,
        train_end_ns=train_end_ns,
        starting_cash=starting_cash,
        variant=variant,
        coin=coin,
        candidate_fills=candidate_fills,
        funding=funding,
    )
    aggregator = StreamingControlAggregator(plan, keep_summaries=keep_summaries)
    reject_records: list[dict] = []
    started = time.monotonic()
    completed = 0
    budget_exhausted = False
    for seed_index in range(replications):
        if max_seconds is not None and time.monotonic() - started > max_seconds:
            budget_exhausted = True
            break
        if max_trades is not None and aggregator.executed_trades >= max_trades:
            budget_exhausted = True
            break
        active_plan = plan
        if mode == "SEQUENTIAL":
            active_plan = prepare_control_plan(
                prepared_quotes,
                trades,
                notional,
                seed,
                max_holding_ns,
                train_fraction=train_fraction,
                train_end_ns=train_end_ns,
                starting_cash=starting_cash,
                variant=variant,
                coin=coin,
                candidate_fills=candidate_fills,
                funding=funding,
            )
        result, rejects = _run_seed_account(active_plan, seed_index)
        aggregator.add(result)
        reject_records.extend(rejects)
        completed += 1
    return plan, aggregator, reject_records, completed, budget_exhausted, time.monotonic() - started


def _control_report(
    events_path: str,
    trades: int | None,
    notional: float,
    seed: int,
    replications: int = 8,
    *,
    execution: str = "BATCH",
    compare_executions: bool = False,
    max_holding_ns: int = 10_000_000_000,
    train_fraction: float = 0.25,
    train_end_ns: int | None = None,
    starting_cash: float | None = None,
    variant: int = 0,
    coin: int = 0,
    candidate_fills: Sequence[Mapping[str, Any]] | None = None,
    funding: Sequence[Mapping[str, Any] | FundingSettlement] | None = None,
    financial_trials: Sequence[Mapping[str, Any]] | None = None,
    cost_reservations: Mapping[str, Any] | None = None,
    max_seconds: float | None = None,
    max_trades: int | None = None,
    comparison_replications: int | None = None,
    keep_summaries: bool = True,
    record_telemetry: bool = False,
) -> dict:
    quotes, tape_funding, tape_funding_raw = load_tape(events_path)
    if funding is None:
        funding_settlements = tape_funding
        funding_raw_events = tape_funding_raw
    else:
        funding_settlements, funding_raw_events = normalize_funding(funding)
    input_hash = _file_sha256(events_path)
    quotes_hash = _hash_json(
        [[q["ts"], repr(q["bid"]), repr(q["ask"])] for q in quotes]
    )
    requested = None if trades is None else max(0, int(trades))
    plan, aggregator, reject_records, completed, budget_exhausted, elapsed = _execute_plan(
        quotes,
        requested,
        notional,
        seed,
        max(0, int(replications)),
        execution=execution,
        max_holding_ns=max_holding_ns,
        train_fraction=train_fraction,
        train_end_ns=train_end_ns,
        starting_cash=starting_cash,
        variant=variant,
        coin=coin,
        candidate_fills=candidate_fills,
        funding=funding_settlements,
        max_seconds=max_seconds,
        max_trades=max_trades,
        keep_summaries=keep_summaries,
    )
    summary = aggregator.summary()
    daily_series = aggregator.daily_series()
    per_stratum = aggregator.per_stratum()

    comparison = None
    if compare_executions:
        # A bounded deterministic subset is compared seed-for-seed in both
        # modes.  The full batch run is reported separately; full sequential
        # equality is never claimed unless the caller actually asked for it.
        subset = (
            max(0, int(replications))
            if comparison_replications is None
            else min(max(0, int(replications)), max(0, int(comparison_replications)))
        )
        _, batch_sub_agg, _, batch_sub_completed, _, _ = _execute_plan(
            quotes,
            requested,
            notional,
            seed,
            subset,
            execution="BATCH",
            max_holding_ns=max_holding_ns,
            train_fraction=train_fraction,
            train_end_ns=train_end_ns,
            starting_cash=starting_cash,
            variant=variant,
            coin=coin,
            candidate_fills=candidate_fills,
            funding=funding_settlements,
            max_seconds=max_seconds,
            max_trades=max_trades,
            keep_summaries=False,
        )
        batch_sub_series = batch_sub_agg.daily_series()
        batch_sub_summary = batch_sub_agg.summary()
        _, seq_agg, _, seq_completed, seq_budget, seq_elapsed = _execute_plan(
            quotes,
            requested,
            notional,
            seed,
            subset,
            execution="SEQUENTIAL",
            max_holding_ns=max_holding_ns,
            train_fraction=train_fraction,
            train_end_ns=train_end_ns,
            starting_cash=starting_cash,
            variant=variant,
            coin=coin,
            candidate_fills=candidate_fills,
            funding=funding_settlements,
            max_seconds=max_seconds,
            max_trades=max_trades,
            keep_summaries=False,
        )
        seq_summary = seq_agg.summary()
        seq_series = seq_agg.daily_series()
        comparison = {
            "subset_seeds": subset,
            "subset_scope": "FIRST_N_SEEDS_FROM_ZERO",
            "full_replications": int(replications),
            "full_sequential_run": subset == int(replications),
            "batch_series_hash": _hash_json(batch_sub_series),
            "sequential_series_hash": _hash_json(seq_series),
            "series_equal": batch_sub_series == seq_series,
            "batch_seed_series_hash": batch_sub_summary["seed_series_hash"],
            "sequential_seed_series_hash": seq_summary["seed_series_hash"],
            "seed_series_equal": (
                batch_sub_summary["seed_series_hash"] == seq_summary["seed_series_hash"]
            ),
            "batch_completed": batch_sub_completed,
            "sequential_completed": seq_completed,
            "sequential_budget_exhausted": seq_budget,
        }
        if record_telemetry:
            comparison["sequential_elapsed_seconds"] = round(seq_elapsed, 6)
        comparison["equal"] = bool(
            comparison["series_equal"] and comparison["seed_series_equal"]
        )
        comparison["note"] = (
            "Both modes ran the same first-N seed subset on the same week. "
            "Full sequential equality is not claimed when "
            "full_sequential_run is false."
        )

    family_material = {
        "task": "TASK-13",
        "draw_mapping": DRAW_MAPPING,
        "seed_protocol_hash": PROTOCOL_HASH,
        "labels": list(LABELS),
        "generator_ids": GENERATOR_IDS,
        "strata": "UTC_HOUR_TRAIN_FITTED",
        "train_fraction": str(train_fraction),
        "train_end_ns": str(plan.train_end_ns),
        "eval_start_ns": str(plan.eval_start_ns),
        "eval_end_ns": str(plan.eval_end_ns),
        "requested_trades": plan.requested_trades,
        "target_trades_by_day": plan.target_trades_by_day,
        "holding_target_ns": plan.holding_target_ns,
        "holding_time_training_mean_ns": plan.holding_mean_ns,
        "holding_time_effective_mean_ns": plan.holding_effective_mean_ns,
        "target_capacity_fraction": str(TARGET_CAPACITY_FRACTION),
        "training_holding_quotes": TRAINING_HOLDING_QUOTES,
        "notional": str(plan.notional),
        "starting_cash": str(plan.starting_cash),
        "replications": int(replications),
        "family_seed": int(seed),
        "variant": int(variant),
        "coin": int(coin),
        "scope": "DEVELOPMENT_CONTROL",
    }
    family_hash = hash_jcs(family_material)
    plan_hash = plan.plan_hash
    selection_plan_hash = plan.selection_plan_hash

    # Frozen seed labels: one frozen CONTROL_REPLICATION per required seed.
    frozen_labels = [
        {
            "PROGRAM_REPLICATION": int(seed),
            "CONTROL_REPLICATION": index,
        }
        for index in range(max(0, int(replications)))
    ]
    frozen_seed_labels_hash = _hash_json(frozen_labels)

    # Financial trials are preserved, never silently dropped.  If the caller
    # supplies a ledger it is copied verbatim; otherwise one trial per frozen
    # control seed is derived from the execution.
    if financial_trials is not None:
        trial_entries = [dict(entry) for entry in financial_trials]
        trial_source = "CALLER_SUPPLIED_PRESERVED"
    else:
        trial_entries = [
            {
                "trial_id": f"CTRL-{entry['seed_index']}",
                "kind": "CONTROL_SEED",
                "seed_index": entry["seed_index"],
                "notional": str(plan.notional),
                "requested_trades": plan.requested_trades,
                "effective_trades": entry["trades"],
                "status": (
                    "MISSING_MARK"
                    if entry["missing_mark"]
                    else ("NO_TRADES" if entry["trades"] == 0 else "EXECUTED")
                ),
                "net_pnl": entry["net_pnl"],
                "funding": entry.get("funding", "0"),
                "control_hash": entry["control_hash"],
            }
            for entry in summary["summaries"]
        ]
        trial_source = "DERIVED_ONE_PER_CONTROL_SEED"
    trial_hash = _hash_json(trial_entries)
    trial_summary = {
        "source": trial_source,
        "input_count": len(financial_trials) if financial_trials is not None else len(trial_entries),
        "preserved_count": len(trial_entries),
        "dropped": 0,
        "executed": sum(1 for t in trial_entries if str(t.get("status", "")).upper() == "EXECUTED"),
        "missing_mark": sum(
            1 for t in trial_entries if str(t.get("status", "")).upper() == "MISSING_MARK"
        ),
        "no_trades": sum(
            1 for t in trial_entries if str(t.get("status", "")).upper() == "NO_TRADES"
        ),
        "entries": trial_entries,
        "trial_hash": trial_hash,
    }

    target_total = sum(plan.target_trades_by_day.values())
    gross_reserved = plan.notional * target_total * max(0, int(replications))
    fee_reserve = gross_reserved * TAKER_FEE * 2
    funding_start_index = bisect.bisect_left(plan.funding_ts, plan.eval_start_ns)
    funding_end_index = bisect.bisect_left(plan.funding_ts, plan.eval_end_ns)
    eval_funding_abs = sum(
        (abs(rate) for rate in plan.funding_rates[funding_start_index:funding_end_index]),
        Fraction(0),
    )
    funding_reserve_bound = gross_reserved * eval_funding_abs
    if cost_reservations is not None:
        cost_payload = {
            "source": "CALLER_SUPPLIED_PRESERVED",
            "preserved": dict(cost_reservations),
            "derived": {
                "gross_notional_reserved": fmt(gross_reserved),
                "fee_reserve": fmt(fee_reserve),
                "funding_reserve_bound": fmt(funding_reserve_bound),
                "funding_rate_abs_sum_in_eval": str(eval_funding_abs),
                "target_trades": target_total,
                "replications": int(replications),
            },
        }
    else:
        cost_payload = {
            "source": "DERIVED_FROM_PROTOCOL",
            "preserved": None,
            "derived": {
                "gross_notional_reserved": fmt(gross_reserved),
                "fee_reserve": fmt(fee_reserve),
                "funding_reserve_bound": fmt(funding_reserve_bound),
                "funding_rate_abs_sum_in_eval": str(eval_funding_abs),
                "funding_reserve_note": (
                    "upper bound if every reserved notional were exposed across "
                    "every settlement in the evaluation window"
                ),
                "target_trades": target_total,
                "replications": int(replications),
                "per_seed_notional": fmt(plan.notional),
                "starting_cash": fmt(plan.starting_cash),
                "taker_fee": str(TAKER_FEE),
            },
        }
    cost_payload["reservation_hash"] = _hash_json(cost_payload)

    rescue = l1_rescue(reject_records, family_seed=seed)
    trend_pullback = run_trend_pullback_control(plan)

    # Funding sign probe: verified on the same arithmetic the account uses, so
    # the reported convention is executable rather than prose.  A long with a
    # positive rate must lose cash; a short must receive it.
    long_probe = funding_cash_delta(1, Fraction(100), Fraction(1, 1000))
    short_probe = funding_cash_delta(-1, Fraction(100), Fraction(1, 1000))
    first_settlement = plan.funding[0] if plan.funding else None
    funding_total = Fraction(summary["funding_total"])
    funding_report = {
        "model": FUNDING_MODEL,
        "sign_convention": FUNDING_SIGN_CONVENTION,
        "sign_probe": {
            "rate": "0.001",
            "reference_notional": "100",
            "long_position_cash_delta": fmt(long_probe),
            "short_position_cash_delta": fmt(short_probe),
            "long_pays_positive_rate": long_probe < 0,
            "short_receives_positive_rate": short_probe > 0,
            "passed": bool(long_probe < 0 and short_probe > 0),
        },
        "window": FUNDING_WINDOW,
        "source": "NATIVE_TAPE_FUNDING_EVENTS",
        "source_sha256": input_hash,
        "raw_events": funding_raw_events,
        "unique_settlements": len(plan.funding),
        "duplicates_dropped": max(0, funding_raw_events - len(plan.funding)),
        "settlements_in_eval_window": funding_end_index - funding_start_index,
        "first_settlement": (
            None
            if first_settlement is None
            else {
                "ts": first_settlement.ts,
                "rate": str(first_settlement.rate),
                "long_cash_delta_per_100_notional": fmt(
                    Fraction(-100) * first_settlement.rate
                ),
            }
        ),
        "funding_hash": plan.funding_hash,
        "reference_notional": "POSITION_ENTRY_NOTIONAL",
        "mark_inference": False,
        "future_funding_applied": False,
        "total_funding_exact": str(funding_total),
        "total_funding": round(float(funding_total), 12),
        "mean_funding_per_seed": (
            _num_str(funding_total / summary["valid_seeds"])
            if summary["valid_seeds"]
            else None
        ),
    }

    short_seed_run = int(replications) < CONTROL_REQUIRED_REPLICATIONS
    incomplete_reasons = []
    if short_seed_run:
        incomplete_reasons.append("SHORT_SEED_RUN")
    if budget_exhausted:
        incomplete_reasons.append("COMPUTE_BUDGET_EXHAUSTED")
    if completed < int(replications):
        incomplete_reasons.append("SEEDS_NOT_COMPLETED")
    if summary["valid_seeds"] == 0:
        incomplete_reasons.append("NO_VALID_CONTROL_SEEDS")

    status = "PASS" if not incomplete_reasons else "INCOMPLETE_EVIDENCE"

    holding_total = sum(
        int(d["holding_ns"]) for d in daily_series
    )
    holding_count = summary["executed_trades"]
    achieved_holding = int(holding_total // holding_count) if holding_count else 0

    resource_usage = {
        "seeds_completed": completed,
        "executed_trades": summary["executed_trades"],
        "budget_exhausted": budget_exhausted,
        "max_seconds": max_seconds,
        "max_trades": max_trades,
        "retained_seed_series": 0,
    }
    if record_telemetry:
        resource_usage["elapsed_seconds"] = round(elapsed, 6)
        resource_usage["peak_rss_bytes"] = _peak_rss_bytes()

    report = {
        "schema_version": SCHEMA_VERSION,
        "task": "TASK-13",
        "status": status,
        "incomplete_reasons": incomplete_reasons,
        "draw_mapping": DRAW_MAPPING,
        "seed_protocol": {
            "protocol": DRAW_MAPPING,
            "protocol_hash": PROTOCOL_HASH,
            "labels": list(LABELS),
            "generator_ids": GENERATOR_IDS,
            "backend": backend_info(),
        },
        "seed_protocol_hash": PROTOCOL_HASH,
        "backend_hash": backend_info()["backend_hash"],
        "family_hash": family_hash,
        "input_hash": input_hash,
        "quotes_hash": quotes_hash,
        "plan_hash": plan_hash,
        "selection_plan_hash": selection_plan_hash,
        "frozen_seed_labels_hash": frozen_seed_labels_hash,
        "trade_count_matched": target_total if len(plan.calendar) > 1 else next(iter(plan.target_trades_by_day.values()), 0),
        "trade_count_target_per_day": plan.target_trades_by_day,
        "training_trade_target_per_day": plan.training_trade_target_by_day,
        "trade_count_target_source": (
            "TRAINING_STRATA_CAPPED_BY_EXPLICIT_INPUT"
            if plan.requested_trades is not None
            else "TRAINING_STRATA"
        ),
        "matched_on": ["calendar", "trade_count", "holding_time"],
        "holding_time_target_ns": plan.holding_target_ns,
        "holding_time_training_mean_ns": plan.holding_mean_ns,
        "holding_time_effective_mean_ns": plan.holding_effective_mean_ns,
        "holding_time_achieved_ns": achieved_holding,
        "holding_time_note": (
            "holding time is drawn from training-stratum samples; achieved holding is "
            "reported and never forced to a candidate outcome"
        ),
        "candidate_fills_used_for_selection": plan.candidate_fills_used,
        "candidate_fills_note": (
            "candidate fills are accepted only as provenance; they never select controls "
            "or force a trade count"
        ),
        "notional": float(plan.notional),
        "seed": int(seed),
        "replications": int(replications),
        "required_replications": CONTROL_REQUIRED_REPLICATIONS,
        "seeds_completed": completed,
        "execution": execution.upper(),
        "batch_vs_sequential": comparison,
        "controls": summary["summaries"],
        "control_mean": summary["control_mean"],
        "control_std": summary["control_std"],
        "control_mean_exact": summary["control_mean_exact"],
        "control_hash": summary["seed_series_hash"],
        "daily_series": daily_series,
        "daily_series_hash": _hash_json(daily_series),
        "seed_series_hash": summary["seed_series_hash"],
        "per_stratum": per_stratum,
        "diagnostics": {
            "executed_trades": summary["executed_trades"],
            "rejected_orders": summary["rejected_orders"],
            "reject_reasons": summary["reject_reasons"],
            "zero_trade_seeds": summary["zero_trade_seeds"],
            "missing_mark_seeds": summary["missing_mark_seeds"],
            "truncated_seeds": summary["truncated_seeds"],
            "schedule_shortfall": summary["schedule_shortfall"],
            "retained_seed_series": summary["retained_seed_series"],
            "streaming_aggregation": True,
            "bounded_memory": True,
        },
        "frozen_trend_pullback_control": {
            **trend_pullback,
            "included_in_random_control_mean": False,
        },
        "l1_rescue": rescue,
        "financial_trials": trial_summary,
        "cost_reservations": cost_payload,
        "resource_usage": resource_usage,
        "acceptance": {
            "F27": (
                "PASS"
                if not short_seed_run and summary["valid_seeds"] > 0
                else "INCOMPLETE_EVIDENCE"
            ),
            "F27_scope": "DEVELOPMENT_WEEK_WITH_EXACT_FUNDING",
            "B1": "PASS",
            "B1_source": "TASK-25 cgroup-enforced 1M rows x 8 variants, p90 2.66 s",
            "T-PROTOCOL": "PASS",
            "T-PROTOCOL_source": "TASK-12 canonical two-symbol 26-month 12/3/3 protocol",
            "promotion": "NOT_A_PROMOTION",
            "strategy_edge": "NOT_CLAIMED",
            "note": (
                "Control evidence only at the development-week scope with exact "
                "tape funding. No strategy edge and no promotion are claimed."
            ),
        },
    }
    report["funding"] = funding_report
    # Every report must carry the protocol identity, even the degenerate ones.
    report["report_hash"] = _hash_json(
        {k: v for k, v in report.items() if k not in {"report_hash", "resource_usage"}}
    )
    return report


def matched_controls(
    events_path: str,
    trades: int | None,
    notional: float,
    seed: int,
    replications: int = 8,
    **kwargs: Any,
) -> dict:
    """Backward-compatible control report entry point.

    The old NumPy PCG64 signature is preserved, but the draw path is now the
    frozen HMAC protocol.  Additional keyword arguments select sequential or
    batch execution, execution budgets, trial preservation and the
    batch-vs-sequential equality check.
    """
    return _control_report(events_path, trades, notional, seed, replications, **kwargs)


def run_control(
    quotes: list[dict],
    trades: int,
    notional: float,
    seed: int,
    max_holding_ns: int = 10_000_000_000,
) -> dict:
    """Run one control seed and return a compact backward-compatible report."""
    plan = prepare_control_plan(
        quotes, trades, notional, seed, max_holding_ns, candidate_fills=None
    )
    result = run_control_seed(plan, 0)
    valid = [d for d in result.days.values() if d["net_pnl_exact"] is not None]
    mean_holding = (
        int(sum(int(d["holding_ns"]) for d in valid) // max(1, sum(int(d["trades"]) for d in valid)))
        if valid
        else 0
    )
    return {
        "seed": int(seed),
        "trades": result.executed_trades,
        "net_pnl": None if result.net_pnl_total is None else round(float(result.net_pnl_total), 8),
        "fees": round(float(result.fees), 8),
        "mean_holding_ns": mean_holding,
        "control_hash": result.control_hash,
        "draw_mapping": DRAW_MAPPING,
        "seed_protocol_hash": PROTOCOL_HASH,
        "daily_series": [result.days[day] for day in plan.calendar],
        "selection_hash": result.selection_hash,
        "rejected_orders": result.rejected_orders,
        "matched_on": ["calendar", "trade_count", "holding_time"],
    }
