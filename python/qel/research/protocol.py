"""Canonical TASK-12 preregistration and walk-forward datasets.

The protocol is deliberately pure Python and deterministic: no wall-clock value,
filesystem object or payload enters a hashed manifest.  The final reserve is
sealed as a hash-only manifest; no research fit path is allowed to read it.

Calendar rules (UTC):

* only complete daily observations are accepted;
* duplicate and misaligned dates are rejected with an explicit code;
* the calendar must be contiguous (a missing day is a calendar gap);
* the final ``ceil(30% * N)`` calendar days are sealed;
* discovery folds are expanding 12 calendar-month training, three calendar
  month validation and a three calendar-month stride.
"""

from __future__ import annotations

import calendar as _month_calendar
import re
from datetime import date, timedelta
from typing import Any, Callable, Mapping, Sequence

from ..contract.canonical import hash_jcs

SCHEMA_VERSION = "1.1.0"
CALENDAR_NAME = "UTC"
DAY_NS = 86_400_000_000_000
SECONDS_PER_DAY = 86_400
RESERVE_NUMERATOR = 3
RESERVE_DENOMINATOR = 10
TRAIN_MONTHS = 12
VALIDATION_MONTHS = 3
STRIDE_MONTHS = 3
REQUIRED_FOLDS = 2
DEFAULT_FAMILY_SIZE = 12

_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_WHOLE_SCOPE_TOKENS = {"ALL", "*", "GLOBAL", "DATASET", "WHOLE_DATASET"}
_EPOCH = date(1970, 1, 1)


class ProtocolError(ValueError):
    """A protocol refusal with a stable, machine-readable code."""

    def __init__(self, code: str, detail: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.detail = dict(detail or {})
        super().__init__(code)

    def as_dict(self) -> dict:
        return {"status": self.code, "code": self.code, "detail": self.detail}


# ------------------------------------------------------------------ calendar --


def _parse_day(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, (bool, int)):
        if isinstance(value, date) and hasattr(value, "hour"):
            raise ProtocolError("INVALID_DATE", {"value": str(value), "reason": "datetime is not a UTC day"})
        return value
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ProtocolError("INVALID_DATE", {"value": repr(value), "expected": "YYYY-MM-DD"})
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ProtocolError("INVALID_DATE", {"value": value, "reason": str(exc)}) from exc
    if parsed.isoformat() != value:
        raise ProtocolError("INVALID_DATE", {"value": value, "reason": "not canonical ISO date"})
    return parsed


def _as_day(value: Any) -> date:
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    return _parse_day(value)


def add_months(day: date, months: int) -> date:
    """Add calendar months, clamping the day to the target month length."""
    year = day.year + (day.month - 1 + months) // 12
    month = (day.month - 1 + months) % 12 + 1
    last = _month_calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def epoch_ns(day: Any) -> int:
    parsed = _as_day(day)
    return (parsed - _EPOCH).days * DAY_NS


def reserve_days(total_days: int) -> int:
    if total_days <= 0:
        return 0
    return (RESERVE_NUMERATOR * total_days + RESERVE_DENOMINATOR - 1) // RESERVE_DENOMINATOR


def _payload_hash(symbol: str, day: str, supplied: Any) -> str:
    if supplied is None:
        return hash_jcs({"symbol": symbol, "date": day, "placeholder": "NO_PAYLOAD_STAGED"})
    if not isinstance(supplied, str) or not _HEX64_RE.match(supplied):
        raise ProtocolError("INVALID_PAYLOAD_HASH", {"symbol": symbol, "date": day})
    return supplied


def _expand_input(payload: Any) -> list[dict]:
    """Normalise supported calendar inputs into observation rows."""
    if isinstance(payload, Mapping):
        if "observations" in payload:
            return _expand_input(payload["observations"])
        if "symbols" in payload:
            rows: list[dict] = []
            symbols = payload["symbols"]
            if not isinstance(symbols, Mapping):
                raise ProtocolError("INVALID_CALENDAR_INPUT", {"reason": "symbols must be an object"})
            for symbol, items in symbols.items():
                rows.extend(_expand_input({"symbol": symbol, "dates": items}))
            return rows
        if "dates" in payload:
            symbol = payload.get("symbol", "DEFAULT")
            items = payload["dates"]
            if isinstance(items, str):
                items = [items]
            if not isinstance(items, Sequence):
                raise ProtocolError("INVALID_CALENDAR_INPUT", {"reason": "dates must be a list"})
            rows = []
            for item in items:
                if isinstance(item, str):
                    rows.append({"symbol": symbol, "date": item})
                elif isinstance(item, Mapping):
                    row = dict(item)
                    row.setdefault("symbol", symbol)
                    rows.append(row)
                else:
                    raise ProtocolError("INVALID_CALENDAR_INPUT", {"reason": "unsupported date row"})
            return _expand_input(rows)
        raise ProtocolError("INVALID_CALENDAR_INPUT", {"reason": "unrecognised calendar object"})
    if isinstance(payload, str):
        return [{"symbol": "DEFAULT", "date": payload}]
    if not isinstance(payload, Sequence):
        raise ProtocolError("INVALID_CALENDAR_INPUT", {"reason": "expected a list or object"})
    rows = []
    default_symbol = "DEFAULT"
    for item in payload:
        if isinstance(item, str):
            rows.append({"symbol": default_symbol, "date": item})
        elif isinstance(item, Mapping):
            day = item.get("date", item.get("day"))
            if day is None:
                raise ProtocolError("INVALID_CALENDAR_INPUT", {"reason": "row has no date"})
            symbol = item.get("symbol", "DEFAULT")
            if not isinstance(symbol, str) or not symbol:
                raise ProtocolError("INVALID_SYMBOL", {"value": repr(symbol)})
            rows.append(
                {
                    "symbol": symbol,
                    "date": day,
                    "complete": item.get("complete", True),
                    "payload_hash": item.get("payload_hash", item.get("hash")),
                }
            )
        else:
            raise ProtocolError("INVALID_CALENDAR_INPUT", {"reason": f"unsupported row {type(item).__name__}"})
    return rows


def _minimum_total_days(required_discovery_days: int) -> int:
    """Smallest N with N - ceil(0.3 N) >= required discovery days."""
    n = required_discovery_days
    while n - reserve_days(n) < required_discovery_days:
        n += 1
    return n


def build_calendar(payload: Any, *, allow_gaps: bool = False) -> dict:
    """Validate complete daily observations and emit the shared UTC calendar.

    ``payload`` accepts ``[{"symbol", "date", "complete", "payload_hash"}]``,
    a list of ISO date strings, ``{"symbols": {sym: [dates]}}`` or
    ``{"symbol": ..., "dates": [...]}``.  A calendar with absent dates is
    rejected by default; ``allow_gaps=True`` records the exact missing dates
    instead, for a declared archive gap.  Every accepted day is still complete.
    """
    rows = _expand_input(payload)
    if not rows:
        raise ProtocolError("EMPTY_CALENDAR", {"reason": "no observations"})

    by_symbol: dict[str, set[str]] = {}
    hashes: dict[str, dict[str, str]] = {}
    for row in rows:
        symbol = row["symbol"]
        day = _parse_day(row["date"]).isoformat()
        if row.get("complete", True) is not True:
            raise ProtocolError("INCOMPLETE_DAY", {"symbol": symbol, "date": day})
        if day in by_symbol.setdefault(symbol, set()):
            raise ProtocolError("DUPLICATE_DATE", {"symbol": symbol, "date": day})
        by_symbol[symbol].add(day)
        hashes.setdefault(day, {})[symbol] = _payload_hash(symbol, day, row.get("payload_hash"))

    symbols = sorted(by_symbol)
    all_dates = sorted(set().union(*(by_symbol[s] for s in symbols)))
    misaligned = {}
    for symbol in symbols:
        missing = sorted(set(all_dates) - by_symbol[symbol])
        if missing:
            misaligned[symbol] = missing[:20]
    if misaligned:
        raise ProtocolError(
            "MISALIGNED_CALENDAR",
            {"missing_dates": misaligned, "missing_counts": {s: len(v) for s, v in misaligned.items()}},
        )

    first = _parse_day(all_dates[0])
    last = _parse_day(all_dates[-1])
    span_days = (last - first).days + 1
    missing_dates: list[str] = []
    if span_days != len(all_dates):
        present = set(all_dates)
        cursor = first
        while cursor <= last:
            text = cursor.isoformat()
            if text not in present:
                missing_dates.append(text)
            cursor += timedelta(days=1)
        if not allow_gaps:
            raise ProtocolError(
                "CALENDAR_GAP", {"missing_days": missing_dates[:50], "missing_count": len(missing_dates)}
            )

    total = len(all_dates)
    reserved_count = reserve_days(total)
    reserved = all_dates[total - reserved_count :]
    discovery = all_dates[: total - reserved_count]
    reserve_hash_map = {day: hashes[day] for day in reserved}
    calendar_hash = hash_jcs(
        {
            "calendar": CALENDAR_NAME,
            "dates": all_dates,
            "hashes": hashes,
            "missing_dates": missing_dates,
        }
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "calendar": CALENDAR_NAME,
        "symbols": symbols,
        "dates": all_dates,
        "day_count": total,
        "calendar_span_days": span_days,
        "calendar_contiguous": not missing_dates,
        "missing_dates": missing_dates,
        "gap_policy": "DECLARED_ARCHIVE_GAPS" if missing_dates else "NONE",
        "first_date": all_dates[0],
        "last_date": all_dates[-1],
        "discovery": {"dates": discovery, "day_count": len(discovery)},
        "reserved": {
            "sealed": True,
            "readable": False,
            "payload_custody": "HASH_ONLY_UNFETCHED",
            "dates": reserved,
            "day_count": reserved_count,
            "hashes": reserve_hash_map,
            "reserve_numerator": RESERVE_NUMERATOR,
            "reserve_denominator": RESERVE_DENOMINATOR,
        },
        "hashes": hashes,
        "calendar_hash": calendar_hash,
    }


# --------------------------------------------------------------- split/folds --


def split_manifest(
    calendar: dict,
    *,
    horizon_ns: int = DAY_NS,
    embargo_ns: int = 0,
    latency_ns: int = 0,
) -> dict:
    """Expand the discovery calendar into eligible walk-forward folds.

    Raises ``ProtocolError("INSUFFICIENT_HISTORY")`` with the exact shortfall
    when fewer than two folds fit.
    """
    if not isinstance(calendar, dict) or "discovery" not in calendar:
        raise ProtocolError("INVALID_CALENDAR", {"reason": "build_calendar output required"})
    discovery = calendar["discovery"]["dates"]
    reserved = calendar["reserved"]["dates"]
    if not discovery:
        raise ProtocolError("INSUFFICIENT_HISTORY", {"available_discovery_days": 0})

    first = _parse_day(discovery[0])
    discovery_end_exclusive = _parse_day(discovery[-1]) + timedelta(days=1)
    cursor = add_months(first, TRAIN_MONTHS)
    folds: list[dict] = []
    index = 0
    while True:
        validation_end_exclusive = add_months(cursor, VALIDATION_MONTHS)
        if validation_end_exclusive > discovery_end_exclusive:
            break
        validation_start_ns = epoch_ns(cursor)
        purge_cutoff_ns = validation_start_ns - embargo_ns
        fold = {
            "fold_id": "",
            "index": index,
            "train_start": discovery[0],
            "train_end": (cursor - timedelta(days=1)).isoformat(),
            "train_end_exclusive": cursor.isoformat(),
            "train_months": TRAIN_MONTHS + index * STRIDE_MONTHS,
            "train_days": (cursor - first).days,
            "validation_start": cursor.isoformat(),
            "validation_end": (validation_end_exclusive - timedelta(days=1)).isoformat(),
            "validation_end_exclusive": validation_end_exclusive.isoformat(),
            "validation_months": VALIDATION_MONTHS,
            "expanding": True,
            "stride_months": STRIDE_MONTHS,
            "cutoffs": {
                "validation_start_ns": str(validation_start_ns),
                "label_purge_ns": str(validation_start_ns),
                "embargo_ns": str(embargo_ns),
                "latency_ns": str(latency_ns),
                "effective_label_cutoff_ns": str(purge_cutoff_ns),
            },
            "purge_rule": "label_end >= validation_start",
            "embargo_rule": "label_end > validation_start - (embargo + latency)",
        }
        fold["fold_id"] = "WF-" + hash_jcs({"fold": fold, "calendar_hash": calendar["calendar_hash"]})[:16].upper()
        folds.append(fold)
        index += 1
        cursor = add_months(cursor, STRIDE_MONTHS)

    if len(folds) < REQUIRED_FOLDS:
        required_end = add_months(first, TRAIN_MONTHS + REQUIRED_FOLDS * STRIDE_MONTHS)
        required_discovery_days = (required_end - first).days
        available = len(discovery)
        shortfall = max(0, required_discovery_days - available)
        raise ProtocolError(
            "INSUFFICIENT_HISTORY",
            {
                "folds_possible": len(folds),
                "folds_required": REQUIRED_FOLDS,
                "available_discovery_days": available,
                "required_discovery_days": required_discovery_days,
                "shortfall_days": shortfall,
                "available_total_days": calendar.get("day_count"),
                "required_total_days": _minimum_total_days(required_discovery_days),
            },
        )

    for fold in folds:
        if fold["train_end_exclusive"] > fold["validation_start"]:
            raise ProtocolError("INVALID_FOLD_BOUNDARY", {"fold_id": fold["fold_id"]})
        if fold["validation_end"] >= reserved[0]:
            raise ProtocolError("SEALED_RESERVE_ACCESS", {"fold_id": fold["fold_id"]})

    body = {
        "schema_version": SCHEMA_VERSION,
        "calendar": CALENDAR_NAME,
        "calendar_hash": calendar["calendar_hash"],
        "protocol": {
            "train_months": TRAIN_MONTHS,
            "validation_months": VALIDATION_MONTHS,
            "stride_months": STRIDE_MONTHS,
            "expanding": True,
            "reserve_fraction": f"{RESERVE_NUMERATOR}/{RESERVE_DENOMINATOR}",
            "folds_required": REQUIRED_FOLDS,
        },
        "folds": folds,
        "fold_count": len(folds),
        "reserved": {
            "sealed": True,
            "readable": False,
            "payload_custody": "HASH_ONLY_UNFETCHED",
            "dates": reserved,
            "day_count": len(reserved),
            "manifest_hash": hash_jcs({"dates": reserved, "hashes": calendar["reserved"]["hashes"]}),
        },
        "horizon_ns": str(horizon_ns),
        "embargo_ns": str(embargo_ns),
        "latency_ns": str(latency_ns),
    }
    split_hash = hash_jcs(body)
    body["split_hash"] = split_hash
    body["split_id"] = "SPLIT-" + split_hash[:16].upper()
    return body


# -------------------------------------------------------------- preregister --


def variant_reservations(family_size: int = DEFAULT_FAMILY_SIZE, seed: int = 20261109) -> list[dict]:
    if family_size != DEFAULT_FAMILY_SIZE:
        raise ProtocolError("INVALID_FAMILY_SIZE", {"expected": DEFAULT_FAMILY_SIZE, "actual": family_size})
    reservations = []
    for index in range(family_size):
        strategy_id = f"STRAT-VAR-{index:02d}"
        reservations.append(
            {
                "variant_index": index,
                "strategy_id": strategy_id,
                "reserved_trials": 1,
                "reservation_id": "TR-" + hash_jcs({"family": "FAM-TASK12", "seed": seed, "variant": strategy_id})[:16].upper(),
                "append_only": True,
                "state": "RESERVED_BEFORE_EXECUTION",
            }
        )
    return reservations


def default_protocol_config() -> dict:
    return {
        "hypothesis": {
            "hypothesis_id": "HYP-EMA-BREAKOUT-MEANREVERSION-01",
            "statement": (
                "A volume-conditioned EMA breakout/mean-reversion family has positive net "
                "expectancy after costs on BTCUSDT perpetual futures across expanding "
                "walk-forward folds and the sealed reserve."
            ),
            "direction": "TWO_SIDED",
        },
        "economic_thresholds": {
            "min_net_pnl_quote": "0.01",
            "min_annualized_return": "0.05",
            "max_drawdown": "0.20",
            "min_trades_per_fold": 30,
            "min_fold_win_fraction": "0.50",
        },
        "selection_rules": {
            "family_wise_alpha": "0.025",
            "multiplicity": "HOLM",
            "primary_metric": "NET_PNL_AFTER_COSTS",
            "promotion_boundary": "SEALED_COHORT_ONLY",
            "research_screen_promotes": False,
        },
        "costs": {
            "fee_bps": "4",
            "slippage_bps": "1",
            "funding": "OBSERVED_VENUE_FUNDING",
            "latency_ns": str(1_000_000),
            "fee_model": "TAKER_BPS",
        },
        "fidelity": {
            "price_path": "OBSERVED_TRADE",
            "book": "SYNTHETIC_L1_AROUND_TRADE",
            "feed_latency": "ASSUMED_ZERO",
            "impact": "NOT_CERTIFIED",
            "claim_boundary": "no observed depth or queue claim",
        },
        "reserve": {
            "fraction": f"{RESERVE_NUMERATOR}/{RESERVE_DENOMINATOR}",
            "sealed": True,
            "payload_custody": "HASH_ONLY_UNFETCHED",
            "readable_by_research": False,
        },
        "execution": {
            "folds_required": REQUIRED_FOLDS,
            "freeze_before_execution": True,
            "trial_policy": "ONE_RESERVED_TRIAL_PER_VARIANT",
        },
        "family": {"family_id": "FAM-TASK12", "family_size": DEFAULT_FAMILY_SIZE, "seed": 20261109},
    }


def preregistration_manifest(
    calendar: dict,
    split: dict,
    *,
    config: Mapping[str, Any] | None = None,
) -> dict:
    """Freeze the hypothesis, rules, costs, fidelity and trial reservation."""
    cfg = default_protocol_config() if config is None else dict(config)
    family = cfg.get("family", {})
    family_size = family.get("family_size", DEFAULT_FAMILY_SIZE)
    seed = family.get("seed", 20261109)
    reservations = variant_reservations(family_size, seed)
    reservation_hash = hash_jcs(reservations)
    cutoffs = [
        {
            "fold_id": fold["fold_id"],
            "validation_start_ns": fold["cutoffs"]["validation_start_ns"],
            "effective_label_cutoff_ns": fold["cutoffs"]["effective_label_cutoff_ns"],
        }
        for fold in split["folds"]
    ]
    body = {
        "schema_version": SCHEMA_VERSION,
        "manifest_kind": "PREREGISTRATION",
        "frozen": True,
        "frozen_before_execution": True,
        "execution_started": False,
        "calendar": CALENDAR_NAME,
        "hypothesis": cfg["hypothesis"],
        "economic_thresholds": cfg["economic_thresholds"],
        "selection_rules": cfg["selection_rules"],
        "costs": cfg["costs"],
        "fidelity": cfg["fidelity"],
        "reserve": cfg["reserve"],
        "execution": cfg["execution"],
        "family": {
            "family_id": family.get("family_id", "FAM-TASK12"),
            "family_size": family_size,
            "seed": seed,
        },
        "trial_reservation": {
            "append_only": True,
            "reserved_trials_total": sum(row["reserved_trials"] for row in reservations),
            "per_variant": reservations,
            "reservation_hash": reservation_hash,
        },
        "input_hashes": {
            "calendar_hash": calendar["calendar_hash"],
            "discovery_dates_hash": hash_jcs(calendar["discovery"]["dates"]),
            "observation_hash_map_hash": hash_jcs(calendar["hashes"]),
            "reserved_manifest_hash": split["reserved"]["manifest_hash"],
            "reserved_dates_hash": hash_jcs(split["reserved"]["dates"]),
        },
        "split": {
            "split_id": split["split_id"],
            "split_hash": split["split_hash"],
            "fold_count": split["fold_count"],
            "fold_hashes": [hash_jcs(fold) for fold in split["folds"]],
            "cutoffs": cutoffs,
            "cutoff_hash": hash_jcs(cutoffs),
        },
    }
    manifest_hash = hash_jcs(body)
    body["manifest_hash"] = manifest_hash
    body["manifest_id"] = "PREREG-" + manifest_hash[:16].upper()
    return body


# ------------------------------------------------------------ purge/embargo --


def _int_ns(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ProtocolError("INVALID_TIMESTAMP", {"field": field, "value": repr(value)})
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise ProtocolError("INVALID_TIMESTAMP", {"field": field, "value": value}) from exc
    raise ProtocolError("INVALID_TIMESTAMP", {"field": field, "value": repr(value)})


def sample_label_end_ns(sample: Mapping[str, Any], horizon_ns: int) -> int:
    if "label_end_ns" in sample:
        return _int_ns(sample["label_end_ns"], "label_end_ns")
    if "decision_ns" in sample:
        return _int_ns(sample["decision_ns"], "decision_ns") + int(horizon_ns)
    if "decision_date" in sample:
        return epoch_ns(sample["decision_date"]) + int(horizon_ns)
    raise ProtocolError("MISSING_LABEL_END", {"sample": dict(sample)})


def purge_training_samples(
    samples: Sequence[Mapping[str, Any]],
    validation_start_ns: int,
    horizon_ns: int,
    *,
    embargo_ns: int = 0,
    latency_ns: int = 0,
) -> dict:
    """Split samples into kept/purged at exact UTC-nanosecond boundaries.

    A sample is purged when ``label_end >= validation_start``.  With an embargo
    or latency, a sample is also purged when
    ``label_end > validation_start - (embargo + latency)``; equality at the
    embargo boundary is allowed.
    """
    validation_start_ns = int(validation_start_ns)
    horizon_ns = int(horizon_ns)
    gap = int(embargo_ns) + int(latency_ns)
    cutoff = validation_start_ns - gap
    kept: list[dict] = []
    purged: list[dict] = []
    for sample in samples:
        end = sample_label_end_ns(sample, horizon_ns)
        entry = {"sample": dict(sample), "label_end_ns": str(end)}
        if end >= validation_start_ns:
            entry["code"] = "LABEL_OVERLAP"
            entry["reason"] = "label_end >= validation_start"
            purged.append(entry)
        elif gap > 0 and end > cutoff:
            entry["code"] = "EMBARGO"
            entry["reason"] = "label_end > validation_start - (embargo + latency)"
            purged.append(entry)
        else:
            kept.append(dict(sample))
    return {
        "rule": "label_end >= validation_start purged; embargo/latency gap applied at equal-time boundary",
        "validation_start_ns": str(validation_start_ns),
        "horizon_ns": str(horizon_ns),
        "embargo_ns": str(embargo_ns),
        "latency_ns": str(latency_ns),
        "effective_label_cutoff_ns": str(cutoff),
        "kept": kept,
        "kept_count": len(kept),
        "purged": purged,
        "purged_count": len(purged),
    }


def purge_fold_samples(
    samples: Sequence[Mapping[str, Any]],
    fold: Mapping[str, Any],
    horizon_ns: int,
    *,
    embargo_ns: int = 0,
    latency_ns: int = 0,
) -> dict:
    return purge_training_samples(
        samples,
        int(fold["cutoffs"]["validation_start_ns"]),
        horizon_ns,
        embargo_ns=embargo_ns,
        latency_ns=latency_ns,
    )


# ---------------------------------------------------------- scoped fit/cache --


def _is_whole_scope(symbol: Any) -> bool:
    return symbol is None or (isinstance(symbol, str) and symbol.upper() in _WHOLE_SCOPE_TOKENS)


def _reserved_set(calendar: Mapping[str, Any] | None) -> set[str]:
    if not calendar:
        return set()
    return set(calendar.get("reserved", {}).get("dates", []))


def _assert_fold_scope(
    fold: Mapping[str, Any],
    calendar: Mapping[str, Any] | None,
) -> None:
    if calendar:
        first = calendar.get("first_date") or (calendar.get("dates") or [None])[0]
        last = calendar.get("last_date") or (calendar.get("dates") or [None])[-1]
        if first and last and fold["train_start"] <= first and fold["train_end"] >= last:
            raise ProtocolError("WHOLE_DATASET_FIT", {"fold_id": fold.get("fold_id")})
        reserved = calendar.get("reserved", {}).get("dates", [])
        if reserved and fold["train_end"] >= reserved[0]:
            raise ProtocolError(
                "SEALED_RESERVE_ACCESS",
                {"fold_id": fold.get("fold_id"), "reserved_first_date": reserved[0]},
            )
    if fold.get("validation_start") and fold.get("train_end_exclusive"):
        if fold["train_end_exclusive"] > fold["validation_start"]:
            raise ProtocolError("FIT_SCOPE_VIOLATION", {"fold_id": fold.get("fold_id")})


def scoped_fit(
    fold: Mapping[str, Any],
    symbol: str,
    samples: Sequence[Mapping[str, Any]],
    *,
    calendar: Mapping[str, Any] | None = None,
    fit_fn: Callable[[list[dict]], Any] | None = None,
) -> Any:
    """Fit only one symbol on one fold's training window.

    Fails closed on a whole-dataset symbol, a fold that spans the whole dataset
    or the sealed reserve, a foreign-symbol sample, a validation/reserved date,
    and a label that reaches the validation boundary.
    """
    if _is_whole_scope(symbol):
        raise ProtocolError("WHOLE_DATASET_FIT", {"symbol": repr(symbol)})
    _assert_fold_scope(fold, calendar)
    reserved = _reserved_set(calendar)
    validation_start_ns = int(fold["cutoffs"]["validation_start_ns"])
    horizon_ns = int(fold["cutoffs"].get("horizon_ns", DAY_NS))
    accepted: list[dict] = []
    for sample in samples:
        sample_symbol = sample.get("symbol")
        if sample_symbol != symbol:
            raise ProtocolError(
                "CROSS_SYMBOL_LEAKAGE",
                {"fit_symbol": symbol, "sample_symbol": sample_symbol, "sample_date": sample.get("date")},
            )
        day = sample.get("date")
        if day is None:
            raise ProtocolError("MISSING_SAMPLE_DATE", {"sample": dict(sample)})
        day = _parse_day(day).isoformat()
        if day in reserved:
            raise ProtocolError("SEALED_RESERVE_ACCESS", {"date": day, "fit_symbol": symbol})
        if not (fold["train_start"] <= day <= fold["train_end"]):
            raise ProtocolError(
                "FIT_SCOPE_VIOLATION",
                {
                    "fit_symbol": symbol,
                    "date": day,
                    "train_start": fold["train_start"],
                    "train_end": fold["train_end"],
                },
            )
        if "label_end_ns" in sample and int(sample["label_end_ns"]) >= validation_start_ns:
            raise ProtocolError("LABEL_OVERLAP", {"date": day, "fit_symbol": symbol})
        accepted.append(dict(sample))
    if fit_fn is not None:
        return fit_fn(accepted)
    return {
        "fold_id": fold.get("fold_id"),
        "symbol": symbol,
        "sample_count": len(accepted),
        "fit_scope": {"train_start": fold["train_start"], "train_end": fold["train_end"]},
        "status": "PASS",
    }


class FoldScopedCache:
    """A cache keyed by (fold_id, symbol); global scopes fail closed."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], Any] = {}

    @staticmethod
    def key(fold: Mapping[str, Any], symbol: str) -> tuple[str, str]:
        if _is_whole_scope(symbol):
            raise ProtocolError("WHOLE_DATASET_CACHE", {"symbol": repr(symbol)})
        fold_id = fold.get("fold_id")
        if not fold_id:
            raise ProtocolError("CACHE_SCOPE_VIOLATION", {"reason": "fold has no fold_id"})
        return (str(fold_id), symbol)

    def put(self, fold: Mapping[str, Any], symbol: str, value: Any) -> None:
        self._store[self.key(fold, symbol)] = value

    def get(self, fold: Mapping[str, Any], symbol: str, default: Any = None) -> Any:
        return self._store.get(self.key(fold, symbol), default)

    def get_global(self, scope: Any = "ALL") -> Any:
        raise ProtocolError("WHOLE_DATASET_CACHE", {"scope": repr(scope)})

    def __len__(self) -> int:
        return len(self._store)


# --------------------------------------------------------------- research IO --


def research_dates(calendar: Mapping[str, Any]) -> list[str]:
    """The only dates a research fit may enumerate: discovery, not reserve."""
    return list(calendar["discovery"]["dates"])


def read_reserved_payload(calendar: Mapping[str, Any], day: str | None = None) -> Any:
    """The reserve is hash-only custody; reading it always fails closed."""
    raise ProtocolError(
        "SEALED_RESERVE_ACCESS",
        {"date": day, "custody": calendar.get("reserved", {}).get("payload_custody", "HASH_ONLY_UNFETCHED")},
    )


# ------------------------------------------------------------- scope checks --


def _expect(code: str, fn: Callable[[], Any]) -> dict:
    try:
        fn()
    except ProtocolError as exc:
        return {"expected": code, "observed": exc.code, "passed": exc.code == code}
    return {"expected": code, "observed": "NO_ERROR", "passed": False}


def _fold_scope_checks(calendar: dict, split: dict, symbol: str | None) -> dict:
    symbols = calendar["symbols"]
    chosen = symbol or symbols[0]
    if chosen not in symbols:
        raise ProtocolError("UNKNOWN_SYMBOL", {"symbol": chosen, "symbols": symbols})
    reserved = calendar["reserved"]["dates"]
    other = next((s for s in symbols if s != chosen), "__CROSS_SYMBOL_SENTINEL__")
    checks: list[dict] = []
    for fold in split["folds"]:
        train_day = fold["train_end"]
        validation_day = fold["validation_start"]
        reserved_day = reserved[0]
        base = {"fold_id": fold["fold_id"]}
        training_result = scoped_fit(
            fold, chosen, [{"symbol": chosen, "date": train_day}], calendar=calendar
        )
        checks.append(
            {
                "name": "training_fit_in_scope",
                "fold_id": fold["fold_id"],
                "expected": "PASS",
                "observed": training_result["status"],
                "passed": training_result["status"] == "PASS",
            }
        )
        checks.append(
            {
                "name": "validation_sample_rejected",
                **base,
                **_expect(
                    "FIT_SCOPE_VIOLATION",
                    lambda f=fold, d=validation_day: scoped_fit(
                        f, chosen, [{"symbol": chosen, "date": d}], calendar=calendar
                    ),
                ),
            }
        )
        checks.append(
            {
                "name": "reserved_sample_rejected",
                **base,
                **_expect(
                    "SEALED_RESERVE_ACCESS",
                    lambda f=fold, d=reserved_day: scoped_fit(
                        f, chosen, [{"symbol": chosen, "date": d}], calendar=calendar
                    ),
                ),
            }
        )
        checks.append(
            {
                "name": "cross_symbol_rejected",
                **base,
                **_expect(
                    "CROSS_SYMBOL_LEAKAGE",
                    lambda f=fold, o=other: scoped_fit(
                        f, chosen, [{"symbol": o, "date": f["train_end"]}], calendar=calendar
                    ),
                ),
            }
        )
        checks.append(
            {
                "name": "cross_symbol_validation_rejected",
                **base,
                **_expect(
                    "CROSS_SYMBOL_LEAKAGE",
                    lambda f=fold, o=other, d=validation_day: scoped_fit(
                        f, chosen, [{"symbol": o, "date": d}], calendar=calendar
                    ),
                ),
            }
        )
        checks.append(
            {
                "name": "cross_symbol_reserve_rejected",
                **base,
                **_expect(
                    "CROSS_SYMBOL_LEAKAGE",
                    lambda f=fold, o=other, d=reserved_day: scoped_fit(
                        f, chosen, [{"symbol": o, "date": d}], calendar=calendar
                    ),
                ),
            }
        )
        whole_fold = dict(fold)
        whole_fold["train_start"] = calendar["first_date"]
        whole_fold["train_end"] = calendar["last_date"]
        checks.append(
            {
                "name": "whole_dataset_fit_rejected",
                **base,
                **_expect(
                    "WHOLE_DATASET_FIT",
                    lambda f=whole_fold: scoped_fit(
                        f, chosen, [{"symbol": chosen, "date": f["train_end"]}], calendar=calendar
                    ),
                ),
            }
        )
        cache = FoldScopedCache()
        cache.put(fold, chosen, {"fold_id": fold["fold_id"]})
        checks.append(
            {
                "name": "fold_cache_roundtrip",
                **base,
                "expected": "PASS",
                "observed": "PASS" if cache.get(fold, chosen) == {"fold_id": fold["fold_id"]} else "FAIL",
                "passed": cache.get(fold, chosen) == {"fold_id": fold["fold_id"]},
            }
        )
        checks.append(
            {
                "name": "whole_dataset_cache_rejected",
                **base,
                **_expect("WHOLE_DATASET_CACHE", lambda c=cache, f=fold: c.get(f, "ALL")),
            }
        )
    passed = all(check["passed"] for check in checks)
    return {
        "status": "PASS" if passed else "FAIL",
        "symbol": chosen,
        "folds_checked": len(split["folds"]),
        "checks": checks,
        "check_count": len(checks),
        "failed_count": sum(1 for check in checks if not check["passed"]),
    }


def protocol_report(
    payload: Any,
    *,
    symbol: str | None = None,
    horizon_ns: int = DAY_NS,
    embargo_ns: int = 0,
    latency_ns: int = 0,
    config: Mapping[str, Any] | None = None,
    include_scope_checks: bool = True,
    allow_gaps: bool = False,
) -> dict:
    """Build the calendar, splits, frozen preregistration and scope checks."""
    calendar: dict | None = None
    try:
        calendar = build_calendar(payload, allow_gaps=allow_gaps)
        split = split_manifest(
            calendar, horizon_ns=horizon_ns, embargo_ns=embargo_ns, latency_ns=latency_ns
        )
        prereg = preregistration_manifest(calendar, split, config=config)
        scope = (
            _fold_scope_checks(calendar, split, symbol)
            if include_scope_checks
            else {"status": "SKIPPED", "folds_checked": 0, "checks": []}
        )
        status = "PASS" if scope["status"] in ("PASS", "SKIPPED") else "FAIL"
        return {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "calendar": calendar,
            "split_manifest": split,
            "preregistration": prereg,
            "scope_checks": scope,
            "resource_usage": {
                "calendar_days": calendar["day_count"],
                "discovery_days": calendar["discovery"]["day_count"],
                "reserved_days": calendar["reserved"]["day_count"],
                "calendar_span_days": calendar["calendar_span_days"],
                "missing_days": len(calendar["missing_dates"]),
                "folds": split["fold_count"],
                "scope_checks": scope.get("check_count", 0),
            },
        }
    except ProtocolError as exc:
        report = {"schema_version": SCHEMA_VERSION, "status": exc.code, "error": exc.as_dict()}
        if calendar is not None:
            report["calendar"] = {
                "day_count": calendar["day_count"],
                "calendar_span_days": calendar["calendar_span_days"],
                "missing_dates": calendar["missing_dates"][:50],
                "first_date": calendar["first_date"],
                "last_date": calendar["last_date"],
                "discovery_days": calendar["discovery"]["day_count"],
                "reserved_days": calendar["reserved"]["day_count"],
                "calendar_hash": calendar["calendar_hash"],
            }
        return report


__all__ = [
    "DAY_NS",
    "DEFAULT_FAMILY_SIZE",
    "FoldScopedCache",
    "ProtocolError",
    "REQUIRED_FOLDS",
    "add_months",
    "build_calendar",
    "default_protocol_config",
    "epoch_ns",
    "preregistration_manifest",
    "protocol_report",
    "purge_fold_samples",
    "purge_training_samples",
    "read_reserved_payload",
    "research_dates",
    "reserve_days",
    "scoped_fit",
    "split_manifest",
    "variant_reservations",
]
