"""Bounded-memory control-plan fitting for full-fold runs.

``qel.research.control.prepare_control_plan`` materialises every quote
of the fold as a ``Quote`` dataclass with ``Fraction`` bid/ask. At fold
scale (456 train+validation days at 86,400 quotes/day = ~39M quotes)
that needs tens of gigabytes and interactive memory the VM does not
have. This module fits the *same frozen plan math* over compact numpy
arrays and reproduces the plan hashes with a streaming sha256 writer.

Every product that the seed runners and the aggregator consume is
available in lazy/compact form:

* strata statistics via counting sorts (holding values and quote
  intervals take a small distinct set; EMA evaluated sequentially with
  the same 1e-9 rational rounding, per-hour counts streaming),
* day/stratum index vectors as numpy arrays,
* `Quote` access as scaled int64 prices converted to exact
  ``Fraction`` per access (identical rationals to the string parse),
* `plan_hash` / `selection_plan_hash` computed byte-identically to
  ``_hash_json`` but without materialising the 39M-integer JSON.

The differential bar is in ``tests/test_control_stream.py``: identical
plan hashes, strata, targets and identical per-seed daily series as
the canonical fit on dev-week and synthetic multi-day fixtures.
"""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from . import control as C

PRICE_SCALE = 100  # tape quote prices are exact at the cent (2dp)
NS_PER_SEC = 1_000_000_000
DAY_NS = 86_400 * NS_PER_SEC


# ---------------------------------------------------------------------------
# Streaming canonical-JSON hash
# ---------------------------------------------------------------------------


class _Chunks:
    """Marker for a huge array of ints streamed in chunks by the writer."""

    def __init__(self, chunks: Iterable[Iterable[int]]):
        self.chunks = chunks


class StreamingHasher:
    """sha256 over the exact bytes ``json.dumps(payload, sort_keys=True,
    separators=(",", ":"))`` produces, streaming huge int lists."""

    def __init__(self):
        self._h = hashlib.sha256()

    def _w(self, text: str) -> None:
        self._h.update(text.encode("utf-8"))

    def _write_str(self, value: str) -> None:
        self._w(json.dumps(value, ensure_ascii=False))

    def write(self, value: Any) -> None:
        if value is None:
            self._w("null")
        elif value is True:
            self._w("true")
        elif value is False:
            self._w("false")
        elif isinstance(value, int):
            self._w(str(int(value)))
        elif isinstance(value, str):
            self._write_str(value)
        elif isinstance(value, _Chunks):
            self._w("[")
            first = True
            for chunk in value.chunks:
                for item in chunk:
                    if not first:
                        self._w(",")
                    first = False
                    self._w(str(int(item)))
            self._w("]")
        elif isinstance(value, (list, tuple)):
            self._w("[")
            for i, item in enumerate(value):
                if i:
                    self._w(",")
                self.write(item)
            self._w("]")
        elif isinstance(value, dict):
            self._w("{")
            keys = sorted(value.keys())
            for i, key in enumerate(keys):
                if i:
                    self._w(",")
                self._write_str(str(key))
                self._w(":")
                self.write(value[key])
            self._w("}")
        else:  # pragma: no cover - failing closed is safer than guessing
            raise TypeError(f"unsupported streaming hash type {type(value)!r}")

    def hexdigest(self) -> str:
        return self._h.hexdigest()


def streaming_hash_json(payload: Any) -> str:
    hasher = StreamingHasher()
    hasher.write(payload)
    return hasher.hexdigest()


def _ints_chunks(values: Sequence[int], chunk: int = 65536) -> Iterable[Iterable[int]]:
    for start in range(0, len(values), chunk):
        yield values[start : start + chunk]


# ---------------------------------------------------------------------------
# Lazy products consumable by the canonical seed runners
# ---------------------------------------------------------------------------


class LazySortedSamples:
    """A sorted sample sequence backed by value counts."""

    def __init__(self, values: Sequence[int], counts: Sequence[int]):
        self._values = tuple(int(v) for v in values)
        self._counts = tuple(int(c) for c in counts)
        self._total = int(sum(self._counts))
        self._cum = []
        running = 0
        for count in self._counts:
            running += count
            self._cum.append(running)
        self._cum_arr = np.array(self._cum, dtype=np.int64)

    def __len__(self) -> int:
        return self._total

    def __getitem__(self, index: Any):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(self._total)))
        i = int(index)
        if i < 0:
            i += self._total
        if i < 0 or i >= self._total:
            raise IndexError(i)
        pos = int(np.searchsorted(self._cum_arr, i, side="right"))
        return self._values[pos]

    def __iter__(self):
        for value, count in zip(self._values, self._counts):
            for _ in range(count):
                yield value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LazySortedSamples n={self._total}>"


class LazyQuotes:
    """Random-access quotes over compact arrays (canonical Quote values)."""

    def __init__(self, ts: np.ndarray, bid_c: np.ndarray, ask_c: np.ndarray):
        self.ts = ts
        self.bid_c = bid_c
        self.ask_c = ask_c

    def __len__(self) -> int:
        return int(len(self.ts))

    def __getitem__(self, index: int) -> C.Quote:
        return C.Quote(
            ts=int(self.ts[index]),
            bid=Fraction(int(self.bid_c[index]), PRICE_SCALE),
            ask=Fraction(int(self.ask_c[index]), PRICE_SCALE),
        )


class Seq:
    """Sequence over a numpy array supporting bool/len/getitem/iter."""

    __slots__ = ("arr",)

    def __init__(self, arr: np.ndarray):
        self.arr = np.asarray(arr)

    def __len__(self) -> int:
        return int(len(self.arr))

    def __bool__(self) -> bool:
        return len(self.arr) > 0

    def __getitem__(self, index: Any) -> Any:
        value = self.arr[index]
        if isinstance(value, np.ndarray):
            return Seq(value)
        return int(value)

    def __iter__(self):
        for value in self.arr.tolist():
            yield int(value)


class StreamPlan:
    """Duck-typed replacement for ``control.ControlPlan`` over arrays."""

    def __init__(self, **fields: Any):
        self.__dict__.update(fields)

    def labels(self, generator: int, time_index: int, control_replication: int) -> dict:
        return {
            "GENERATOR_ID": generator,
            "PROGRAM_REPLICATION": self.family_seed,
            "VARIANT": self.variant,
            "COIN": self.coin,
            "TIME": time_index,
            "CONTROL_REPLICATION": control_replication,
        }


# ---------------------------------------------------------------------------
# Quote sidecar extraction
# ---------------------------------------------------------------------------


def price_to_cents(text: str) -> int:
    """Exact cents for a decimal price string; fail closed above 2dp."""
    neg = text.startswith("-")
    body = text[1:] if neg and not text.startswith("--") else text
    if "." in body:
        whole, frac = body.split(".", 1)
        if len(frac) > 2:
            raise ValueError(f"PRICE_SCALE_EXCEEDED {text!r}")
        frac = (frac + "00")[:2]
    else:
        whole, frac = body, "00"
    value = int(whole or "0") * PRICE_SCALE + int(frac)
    return -value if neg else value


def extract_day_quotes(tape_path: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Stream a built day tape into (ts, bid_cents, ask_cents) arrays."""
    ts_list: list[int] = []
    bid_list: list[int] = []
    ask_list: list[int] = []
    with open(tape_path, "r", encoding="utf-8") as handle:
        for line in handle:
            if '"kind": "Quote"' not in line:
                continue
            rec = json.loads(line)
            ts_list.append(int(rec["ts_ns"]))
            bid_list.append(price_to_cents(rec["bid"]))
            ask_list.append(price_to_cents(rec["ask"]))
    order = np.argsort(np.array(ts_list, dtype=np.int64), kind="stable")
    ts = np.array(ts_list, dtype=np.int64)[order]
    bid = np.array(bid_list, dtype=np.int64)[order]
    ask = np.array(ask_list, dtype=np.int64)[order]
    return ts, bid, ask


# ---------------------------------------------------------------------------
# Exact order statistics over value counts
# ---------------------------------------------------------------------------


def _counts_median(values: Sequence[int], counts: Sequence[int]) -> int:
    total = int(sum(counts))
    # Middle position(s) under _median's definition on the sorted list.
    if total % 2:
        target = total // 2
        need = [target]
    else:
        need = [total // 2 - 1, total // 2]
    found: dict[int, int] = {}
    running = 0
    for value, count in zip(values, counts):
        running += int(count)
        for target in need:
            if target not in found and running > target:
                found[target] = int(value)
        if len(found) == len(need):
            break
    if len(need) == 1:
        return found[need[0]]
    return (found[need[0]] + found[need[1]]) // 2


def _counts_mean(values: Sequence[int], counts: Sequence[int]) -> int:
    total = int(sum(counts))
    return sum(int(v) * int(c) for v, c in zip(values, counts)) // total


# ---------------------------------------------------------------------------
# Bounded fit
# ---------------------------------------------------------------------------


def fit_control_plan_stream(
    day_arrays: Sequence[tuple[str, np.ndarray, np.ndarray, np.ndarray]],
    *,
    trades: int | None,
    notional: Any,
    seed: int,
    max_holding_ns: int,
    train_end_ns: int,
    starting_cash: Any = None,
    variant: int = 0,
    coin: int = 0,
    funding: Sequence[Any] = (),
) -> StreamPlan:
    """Fit the frozen control plan over per-day quote arrays.

    ``day_arrays`` are ``(day, ts, bid_cents, ask_cents)`` in ascending
    timestamp order across days. The result matches
    ``prepare_control_plan`` product-for-product (see module docstring).
    """
    notional_f = Fraction(str(notional))
    if starting_cash is None:
        starting_cash_f = max(notional_f * 10, Fraction(1000))
    else:
        starting_cash_f = Fraction(str(starting_cash))
    requested = None if trades is None else max(0, int(trades))

    # Global concatenation offsets (days already in calendar order).
    day_offsets: dict[str, tuple[int, int]] = {}
    ts_parts: list[np.ndarray] = []
    bid_parts: list[np.ndarray] = []
    ask_parts: list[np.ndarray] = []
    offset = 0
    for day, ts, bid, ask in day_arrays:
        day_offsets[day] = (offset, offset + len(ts))
        ts_parts.append(ts)
        bid_parts.append(bid)
        ask_parts.append(ask)
        offset += len(ts)
    ts_all = np.concatenate(ts_parts) if ts_parts else np.zeros(0, dtype=np.int64)
    bid_all = np.concatenate(bid_parts) if bid_parts else np.zeros(0, dtype=np.int64)
    ask_all = np.concatenate(ask_parts) if ask_parts else np.zeros(0, dtype=np.int64)
    total_quotes = int(len(ts_all))
    if total_quotes < 4:
        raise ValueError("INSUFFICIENT_HISTORY")

    train_end_ns = int(train_end_ns)
    split = int(np.searchsorted(ts_all, train_end_ns, side="right"))
    train_idx = np.arange(0, split, dtype=np.int64)
    eval_idx = np.arange(split, total_quotes, dtype=np.int64)
    if len(train_idx) < 2 or len(eval_idx) < 2:
        # Canonical degenerate fallback: first half of the tape is train.
        cut = max(2, total_quotes // 2)
        train_idx = np.arange(0, cut, dtype=np.int64)
        eval_idx = np.arange(cut, total_quotes, dtype=np.int64)
        train_end_ns = int(ts_all[cut - 1])
        split = cut
    if len(train_idx) < 2 or len(eval_idx) < 2:
        raise C.ControlError("INSUFFICIENT_HISTORY")

    # Hours for every quote (int8).
    hours_all = (((ts_all // NS_PER_SEC) % 86400) // 3600).astype(np.int64)

    # --- Training holding records (k=2 lookahead), value counts ---------
    train_ts = ts_all[train_idx]
    if len(train_ts) >= 3:
        deltas = np.empty(len(train_ts) - 1, dtype=np.int64)
        deltas[:-1] = train_ts[2:] - train_ts[:-2]
        deltas[-1] = train_ts[-1] - train_ts[-2]
    else:
        deltas = np.empty(0, dtype=np.int64)
    holding = np.clip(deltas, 1, int(max_holding_ns)).astype(np.int64)
    train_hours = hours_all[train_idx][:-1] if len(train_ts) else np.zeros(0, dtype=np.int64)
    if len(holding) and not len(train_hours) == len(holding):
        raise ValueError("holding/hour length mismatch")
    if len(holding) == 0:
        raise C.ControlError("INSUFFICIENT_TRAINING_HISTORY")
    g_values, g_counts = np.unique(holding, return_counts=True)
    global_target = _counts_median(g_values, g_counts)
    global_mean = _counts_mean(g_values, g_counts)
    intervals = np.diff(train_ts)
    if len(intervals):
        i_values, i_counts = np.unique(intervals, return_counts=True)
        median_interval = _counts_median(i_values, i_counts)
    else:
        median_interval = 0
    effective_mean = global_mean + median_interval

    # --- Per-hour counts, EMA long/directional signals ------------------
    train_hours_full = hours_all[train_idx]
    eval_hours_full = hours_all[eval_idx]
    train_by_hour_count = np.zeros(24, dtype=np.int64)
    np.add.at(train_by_hour_count, train_hours_full, 1)
    eval_by_hour_count = np.zeros(24, dtype=np.int64)
    np.add.at(eval_by_hour_count, eval_hours_full, 1)
    long_counts = np.zeros(24, dtype=np.int64)
    directional_counts = np.zeros(24, dtype=np.int64)
    _ema_long_counts(bid_all[train_idx], ask_all[train_idx], train_hours_full, long_counts, directional_counts)

    # --- Per-hour holding distributions ---------------------------------
    hour_dist: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for hour in range(24):
        mask = train_hours == hour
        if mask.any():
            hour_dist[hour] = np.unique(holding[mask], return_counts=True)

    # --- Strata objects --------------------------------------------------
    total_train = int(len(train_idx))
    strata: dict[str, C.StratumModel] = {}
    hours = sorted(set(int(h) for h in np.unique(np.concatenate([train_hours_full, eval_hours_full]))))
    for hour in hours:
        key = f"H{hour:02d}"
        weight = Fraction(int(train_by_hour_count[hour]), total_train) if total_train else Fraction(0)
        if hour in hour_dist:
            h_values, h_counts = hour_dist[hour]
        else:
            h_values, h_counts = g_values, g_counts
        target = _counts_median(h_values, h_counts)
        long_count = int(long_counts[hour])
        long_total = int(directional_counts[hour])
        long_rate = Fraction(long_count, long_total) if long_total else Fraction(1, 2)
        strata[key] = C.StratumModel(
            key=key,
            hour=hour,
            weight=weight if weight > 0 else Fraction(0),
            long_rate=long_rate,
            holding_samples=LazySortedSamples(h_values, h_counts),
            holding_target_ns=int(target),
            train_quotes=int(train_by_hour_count[hour]),
            eval_quotes=int(eval_by_hour_count[hour]),
        )
    supported = [s for s in strata.values() if s.weight > 0]
    total_weight = sum((s.weight for s in supported), Fraction(0))
    if total_weight > 0 and total_weight != 1:
        strata = {
            key: (
                C.StratumModel(
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

    # --- Calendar, day ranges, eval groups -------------------------------
    calendar = tuple(sorted(day_offsets.keys()))
    eval_by_day: dict[str, np.ndarray] = {}
    eval_by_stratum: dict[str, dict[str, np.ndarray]] = {}
    eval_ts_by_stratum: dict[str, dict[str, np.ndarray]] = {}
    for day in calendar:
        start, end = day_offsets[day]
        lo = max(start, split)
        if lo < end:
            eval_by_day[day] = Seq(np.arange(lo, end, dtype=np.int64))
            day_eval_ts = ts_all[lo:end]
            day_eval_hours = hours_all[lo:end]
            groups: dict[str, np.ndarray] = {}
            ts_groups: dict[str, np.ndarray] = {}
            for hour in sorted(set(int(h) for h in np.unique(day_eval_hours))):
                key = f"H{hour:02d}"
                mask = day_eval_hours == hour
                positions = np.flatnonzero(mask)
                groups[key] = Seq((positions + lo).astype(np.int64))
                ts_groups[key] = Seq(day_eval_ts[mask])
            eval_by_stratum[day] = groups
            eval_ts_by_stratum[day] = ts_groups

    # --- Per-day targets --------------------------------------------------
    target_by_day: dict[str, int] = {}
    training_target_by_day: dict[str, int] = {}
    for day in calendar:
        day_eval = eval_by_day.get(day)
        if day_eval is None or len(day_eval) == 0:
            target_by_day[day] = 0
            training_target_by_day[day] = 0
            continue
        first = int(ts_all[day_eval[0]])
        last = int(ts_all[day_eval[-1]])
        duration = max(0, last - first)
        training_target = (
            max(
                1,
                int(
                    C._ceil_int(
                        Fraction(duration)
                        * C.TARGET_CAPACITY_FRACTION
                        / max(1, effective_mean)
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

    # --- Funding -----------------------------------------------------------
    funding_settlements, funding_raw_events = C.normalize_funding(funding or ())
    funding_ts = tuple(item.ts for item in funding_settlements)
    funding_rates = tuple(item.rate for item in funding_settlements)
    funding_hash = C._hash_json([[item.ts, str(item.rate)] for item in funding_settlements])

    # --- Hashes (streaming, byte-identical to _hash_json) ----------------
    def _sorted_chunks(values: np.ndarray) -> Iterable[Iterable[int]]:
        # Values are already ascending; chunk without materialising lists.
        arr = np.asarray(getattr(values, "arr", values), dtype=np.int64)
        for start in range(0, len(arr), 65536):
            yield arr[start : start + 65536].tolist()

    strata_material = {}
    for key, s in sorted(strata.items()):
        if s.hour in hour_dist:
            h_values, h_counts = hour_dist[s.hour]
        else:
            h_values, h_counts = g_values, g_counts
        samples_hash = streaming_hash_json(_Chunks(_repeat_chunks(h_values, h_counts)))
        strata_material[key] = {
            "weight": str(s.weight),
            "long_rate": str(s.long_rate),
            "holding_target_ns": s.holding_target_ns,
            "holding_samples_hash": samples_hash,
            "train_quotes": s.train_quotes,
        }
    def _selection_material() -> dict[str, Any]:
        # Fresh chunk generators on every call: a generator-backed _Chunks
        # can be streamed exactly once, and the plan hash re-streams the
        # selection material, so each pass needs its own.
        return {
            "strata": strata_material,
            "calendar": list(calendar),
            "eval_indices": {day: _Chunks(_sorted_chunks(idxs)) for day, idxs in sorted(eval_by_day.items())},
            "target_trades_by_day": target_by_day,
            "training_trade_target_by_day": training_target_by_day,
            "requested_trades": requested,
            "max_holding_ns": int(max_holding_ns),
            "train_end_ns": train_end_ns,
            "holding_mean_ns": int(global_mean),
            "holding_effective_mean_ns": int(effective_mean),
            "variant": int(variant),
            "coin": int(coin),
        }

    selection_plan_hash = streaming_hash_json(_selection_material())
    plan_material = {
        "draw_mapping": C.DRAW_MAPPING,
        "seed_protocol_hash": C.PROTOCOL_HASH,
        "generator_ids": C.GENERATOR_IDS,
        "family_seed": int(seed),
        **_selection_material(),
        "notional": str(notional_f),
        "starting_cash": str(starting_cash_f),
        "funding_model": C.FUNDING_MODEL,
        "funding_sign_convention": C.FUNDING_SIGN_CONVENTION,
        "funding_window": C.FUNDING_WINDOW,
        "funding_hash": funding_hash,
    }
    plan_hash = streaming_hash_json(plan_material)

    eval_start_ns = int(ts_all[eval_idx[0]]) if len(eval_idx) else 0
    eval_end_ns = int(ts_all[eval_idx[-1]]) if len(eval_idx) else 0
    day_indices = {
        day: np.arange(day_offsets[day][0], day_offsets[day][1], dtype=np.int64) for day in calendar
    }
    return StreamPlan(
        quotes=LazyQuotes(ts_all, bid_all, ask_all),
        calendar=calendar,
        day_indices=day_indices,
        eval_indices=eval_by_day,
        eval_indices_by_stratum=eval_by_stratum,
        eval_ts_by_stratum=eval_ts_by_stratum,
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
        holding_target_ns=int(global_target),
        holding_mean_ns=int(global_mean),
        holding_effective_mean_ns=int(effective_mean),
        plan_hash=plan_hash,
        selection_plan_hash=selection_plan_hash,
        candidate_fills_used=False,
        funding=funding_settlements,
        funding_ts=funding_ts,
        funding_rates=funding_rates,
        funding_hash=funding_hash,
        funding_raw_events=funding_raw_events,
    )


def _repeat_chunks(values: np.ndarray, counts: np.ndarray) -> Iterable[Iterable[int]]:
    for value, count in zip(np.asarray(values).tolist(), np.asarray(counts).tolist()):
        remaining = int(count)
        while remaining > 0:
            step = min(remaining, 65536)
            yield [int(value)] * step
            remaining -= step


def _ema_long_counts(
    bid_c: np.ndarray,
    ask_c: np.ndarray,
    hours: np.ndarray,
    long_counts: np.ndarray,
    directional_counts: np.ndarray,
) -> None:
    """Sequential EMA(20) with the canonical 1e-9 rational rounding."""
    if len(bid_c) == 0:
        return
    quantum = Fraction(1, 1_000_000_000)
    alpha = Fraction(2, 21)
    mid0 = Fraction(int(bid_c[0]) + int(ask_c[0]), 2 * PRICE_SCALE)
    state = mid0
    # First step includes i=0 exactly like the canonical loop.
    alpha_state_coeff = Fraction(1) - alpha
    for i in range(len(bid_c)):
        mid = Fraction(int(bid_c[i]) + int(ask_c[i]), 2 * PRICE_SCALE)
        state = alpha * mid + alpha_state_coeff * state
        state = Fraction(round(state / quantum)) * quantum
        hour = int(hours[i])
        if mid > state:
            long_counts[hour] += 1
        if mid != state:
            directional_counts[hour] += 1
