#!/usr/bin/env python3
"""Read-only capability probe for the pinned native backend and the HftBacktest candidate.

This script does not modify either checkout, does not build anything, and does not
claim any capability. It records the exact commit/tag of each source tree and checks
for the presence or absence of the hooks named in the probe brief (issues #3, #4,
#10, #11).

The ``status`` field is the probe author's reading of the source. The ``matches``
field only says whether the mechanical symbol check agrees with that reading; it is
not a capability certificate.

Usage:
    python3 scripts/probe_native_capabilities.py \
        --pinned /home/quant/quant/src-clones/nautilus \
        --candidate /home/quant/quant/src-clones/hftbacktest \
        --out evidence/native/CAPABILITY_PROBE_2026-11-09.json

Exit codes:
    0  probe ran and both expected commits were verified
    1  a required source tree, tag, or commit could not be verified
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PINNED_COMMIT = "27a8e54e7ac3c57d6cbf8891f0283dfbaee97317"
PINNED_TAG = "v1.231.0"
CANDIDATE_COMMIT = "20cd9470a431e90c526eca6975ef389073c9aca5"
CANDIDATE_TAG = "v1.8.4"

# Each check: (hook, status, relative path, symbol expected present, symbol expected absent)
# An empty string skips that side of the check. A relative path may be a file or directory.
PINNED_CHECKS = [
    ("liquidation_venue_config", "PRESENT", "crates/backtest/src/config.rs", "liquidation_enabled", ""),
    ("liquidation_engine_step", "PRESENT", "crates/backtest/src/engine.rs", "run_venue_liquidations", ""),
    ("liquidation_exchange", "PRESENT", "crates/backtest/src/exchange.rs", "process_liquidations", ""),
    ("liquidation_matching_engine", "PRESENT", "crates/execution/src/matching_engine/engine.rs", "liquidate_open_positions", ""),
    ("liquidation_maintenance_margin", "PARTIAL", "crates/model/src/accounts/margin.rs", "total_maintenance_margin", ""),
    ("liquidation_quote_based_trigger", "PARTIAL", "crates/common/src/cache/mod.rs", "let Some(quote) = self.quote(&position.instrument_id)", ""),
    ("l2_queue_position", "PRESENT", "crates/execution/src/matching_engine/engine.rs", "snapshot_queue_position", ""),
    ("l2_queue_shared_budget", "PRESENT", "crates/execution/src/matching_engine/engine.rs", "decrement_queue_on_trade", ""),
    ("l2_liquidity_consumption", "PRESENT", "crates/execution/src/matching_engine/engine.rs", "apply_liquidity_consumption", ""),
    ("l2_exact_level_qty", "PRESENT", "crates/model/src/orderbook/book.rs", "get_quantity_at_level", ""),
    ("local_view_feed_latency", "MISSING", "crates/execution/src/models/latency.rs", "", "feed_latency"),
    ("local_view_immutable_type", "MISSING", "crates/trading/src/strategy/mod.rs", "", "LocalView"),
    ("local_view_shared_cache", "PRESENT_SHARED_CACHE", "crates/common/src/actor/data_actor.rs", "fn cache(&self)", ""),
]

CANDIDATE_CHECKS = [
    ("queue_model_trait", "PRESENT", "rust/src/backtest/models/queue.rs", "pub trait QueueModel", ""),
    ("queue_model_risk_adverse", "PRESENT", "rust/src/backtest/models/queue.rs", "RiskAdverseQueueModel", ""),
    ("queue_model_probability", "PRESENT", "rust/src/backtest/models/queue.rs", "ProbQueueModel", ""),
    ("queue_own_consumption_budget", "MISSING", "rust/src/backtest/proc/partialfillexchange.rs", "", "consum"),
    ("local_processor", "PRESENT", "rust/src/backtest/proc/local.rs", "pub struct Local<", ""),
    ("local_feed_latency_observation", "PRESENT", "rust/src/backtest/proc/local.rs", "fn feed_latency(&self)", ""),
    ("local_order_latency", "PRESENT", "rust/src/backtest/models/latencies.rs", "pub trait LatencyModel", ""),
    ("local_view_immutable_type", "MISSING", "rust/src/backtest", "", "LocalView"),
    ("margin_or_liquidation", "MISSING", "rust/src/backtest", "", "liquidation"),
    ("streaming_chunked_reader", "PRESENT", "rust/src/backtest/reader.rs", "pub fn release(&mut self", ""),
    ("state_purge_closed_export", "MISSING", "rust/src/backtest", "", "purge"),
]


def git(repo: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def count_symbol(root: Path, rel_path: str, symbol: str) -> tuple[bool, int]:
    path = root / rel_path
    if not path.exists():
        return False, 0
    if path.is_dir():
        total = 0
        for child in sorted(path.rglob("*")):
            if child.is_file():
                total += read_text(child).count(symbol)
        return True, total
    return True, read_text(path).count(symbol)


def probe_tree(root: Path, expected_commit: str, expected_tag: str, checks: list) -> dict:
    code_head, head = git(root, "rev-parse", "HEAD")
    code_tag, tag = git(root, "describe", "--tags", "--exact-match")
    hooks = []
    for hook, status, rel_path, present_symbol, absent_symbol in checks:
        exists_p, present_hits = count_symbol(root, rel_path, present_symbol) if present_symbol else (True, 0)
        exists_a, absent_hits = count_symbol(root, rel_path, absent_symbol) if absent_symbol else (True, 0)
        present_ok = bool(present_symbol) and exists_p and present_hits > 0
        absent_ok = (not absent_symbol) or (exists_a and absent_hits == 0)
        if status == "PRESENT":
            matches = present_ok and absent_ok
        elif status == "MISSING":
            matches = absent_ok and (present_hits == 0)
        else:
            matches = present_ok and absent_ok
        hooks.append(
            {
                "hook": hook,
                "status": status,
                "path": rel_path,
                "symbol_present": present_symbol or None,
                "symbol_present_hits": present_hits if present_symbol else None,
                "symbol_absent": absent_symbol or None,
                "symbol_absent_hits": absent_hits if absent_symbol else None,
                "matches": matches,
            }
        )
    return {
        "root": str(root),
        "head": head if code_head == 0 else None,
        "tag": tag if code_tag == 0 else None,
        "head_matches_expected": code_head == 0 and head == expected_commit,
        "tag_matches_expected": code_tag == 0 and tag == expected_tag,
        "hooks": hooks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pinned", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    pinned = probe_tree(args.pinned, PINNED_COMMIT, PINNED_TAG, PINNED_CHECKS)
    candidate = probe_tree(args.candidate, CANDIDATE_COMMIT, CANDIDATE_TAG, CANDIDATE_CHECKS)
    license_path = args.candidate / "LICENSE"

    report = {
        "probe": "native-capability-probe",
        "date": "2026-11-09",
        "read_only": True,
        "does_not_change_capability_claims": True,
        "pinned": pinned,
        "candidate": candidate,
        "candidate_license": read_text(license_path).splitlines()[0] if license_path.is_file() else None,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(args.out),
                "pinned_ok": pinned["head_matches_expected"],
                "candidate_ok": candidate["head_matches_expected"],
            }
        )
    )
    return 0 if (pinned["head_matches_expected"] and candidate["head_matches_expected"]) else 1


if __name__ == "__main__":
    sys.exit(main())
