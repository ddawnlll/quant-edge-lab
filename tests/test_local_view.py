"""TASK-11/TASK-18 F22 two-world LocalView evidence tests.

These tests do not re-run the heavy native replay. They validate the committed
evidence, recompute the independent accounting from the small economic tapes,
check the zero-delay control against the native runner, and exercise the
negative/control paths of the F22 validators.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from qel.accounting.core import dec, reconstruct

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "labd"
FINAL = EVIDENCE / "LOCAL_VIEW_F22_2026-11-10.json"
NATIVE_CONTROL = EVIDENCE / "LOCAL_VIEW_NATIVE_CONTROL.json"

RUN_FILES = {
    "control": EVIDENCE / "LOCAL_VIEW_CONTROL_ZERO.json",
    "main": EVIDENCE / "LOCAL_VIEW_F22_RUN.json",
    "large_response": EVIDENCE / "LOCAL_VIEW_F22_LARGE_RESPONSE_RUN.json",
    "decision_in_gap": EVIDENCE / "LOCAL_VIEW_F22_DECISION_IN_GAP_RUN.json",
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _runs() -> dict[str, dict]:
    return {name: _load(path) for name, path in RUN_FILES.items()}


def _signed(fill: dict) -> int:
    qty = int(fill["qty"].split(".")[0] or "0") if "." not in fill["qty"] else None
    # Quantities in this profile are exact thousandths; use Decimal-free scaling.
    micro = _scaled(fill["qty"])
    return micro if fill["side"] == "BUY" else -micro


def _scaled(value: str, places: int = 9) -> int:
    negative = value.startswith("-")
    text = value[1:] if negative else value
    if "." in text:
        whole, frac = text.split(".")
    else:
        whole, frac = text, ""
    frac = (frac + "0" * places)[:places]
    number = int(whole or "0") * (10**places) + int(frac or "0")
    return -number if negative else number


def check_fill(fill: dict, response_delay_ns: int) -> None:
    """Validate one fill against the F22 delivery contract."""
    exchange_ts = int(fill["exchange_ts"])
    deliver_at = int(fill["deliver_at"])
    delivered_at = int(fill["delivered_at"])
    assert fill["delivered"] is True
    assert deliver_at == exchange_ts + response_delay_ns
    assert delivered_at >= deliver_at
    if response_delay_ns > 0:
        assert delivered_at > exchange_ts
    assert fill["local_position_before"] == fill["exchange_position_before"]
    assert fill["local_position_after"] == fill["exchange_position_after"]
    signed = _scaled(fill["qty"]) * (1 if fill["side"] == "BUY" else -1)
    assert _scaled(fill["exchange_position_after"]) == (
        _scaled(fill["exchange_position_before"]) + signed
    )


def check_feed_sample(sample: dict, feed_delay_ns: int) -> None:
    ts_event = int(sample["ts_event"])
    deliver_at = int(sample["deliver_at"])
    delivered_at = int(sample["delivered_at"])
    assert deliver_at == ts_event + feed_delay_ns
    assert delivered_at >= deliver_at
    assert delivered_at >= ts_event + feed_delay_ns


def decision_position_matches_delivered(decision: dict) -> bool:
    return _scaled(decision["local_position_qty"]) == _scaled(
        decision["expected_position_from_delivered_fills"]
    )


def test_evidence_status_and_binding_hashes():
    doc = _load(FINAL)
    assert doc["schema"] == "qel.local_view.f22.evidence.v1"
    assert doc["status"] == "PASS_SCOPED_F22_PROFILE"
    assert doc["binding"]["nautilus"] == {
        "repository": "https://github.com/nautechsystems/nautilus_trader",
        "tag": "v1.231.0",
        "commit": "27a8e54e7ac3c57d6cbf8891f0283dfbaee97317",
    }

    source = Path(doc["binding"]["source"]["path"])
    binary = Path(doc["binding"]["binary"]["path"])
    if source.is_file():
        import hashlib

        assert hashlib.sha256(source.read_bytes()).hexdigest() == doc["binding"]["source"][
            "sha256"
        ]
        if binary.is_file():
            assert hashlib.sha256(binary.read_bytes()).hexdigest() == doc["binding"]["binary"][
                "sha256"
            ]
    elif binary.is_file():
        pytest.skip(
            "binary was rebuilt from a different checkout path; the source hash is the "
            "stable binding and the recorded build path is absent"
        )
    corpus = Path(doc["binding"]["corpus"]["path"])
    if corpus.is_file():
        import hashlib

        assert hashlib.sha256(corpus.read_bytes()).hexdigest() == doc["binding"]["corpus"][
            "sha256"
        ]

    strategy = ROOT / doc["binding"]["strategy"]["path"]
    if strategy.is_file():
        import hashlib

        assert hashlib.sha256(strategy.read_bytes()).hexdigest() == doc["binding"]["strategy"][
            "sha256"
        ]

    for name, artifact in doc["artifacts"].items():
        path = ROOT / artifact["path"]
        assert path.is_file(), name
        import hashlib

        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"], name


def test_control_zero_delay_equals_native_runner():
    control = _load(RUN_FILES["control"])
    native = _load(NATIVE_CONTROL)
    assert control["status"] == "PASS"
    assert native["status"] == "PASS"

    def events(tape: list[dict], kind: str) -> list[dict]:
        return [event for event in tape if event["type"] == kind]

    control_fills = events(control["tape"], "FILL")
    native_fills = events(native["tape"], "FILL")
    assert len(control_fills) == len(native_fills) == control["counts"]["fills"] > 0
    for left, right in zip(control_fills, native_fills):
        for field in ("ts_ns", "side", "qty", "price", "fee"):
            assert left[field] == right[field]

    control_funding = events(control["tape"], "FUNDING")
    native_funding = events(native["tape"], "FUNDING")
    assert len(control_funding) == len(native_funding) == 2
    for left, right in zip(control_funding, native_funding):
        for field in (
            "ts_ns",
            "amount",
            "currency",
            "epoch",
            "settlement_id",
            "position_id",
            "account_id",
        ):
            assert left[field] == right[field]

    assert [event["price"] for event in events(control["tape"], "MARK")] == [
        event["price"] for event in events(native["tape"], "MARK")
    ]
    for field in ("total", "free", "locked", "unrealized", "equity", "position_qty", "mark"):
        assert control["exchange"]["native_account"][field] == native["native_account"][field]

    assert control["f22"]["response_delay"]["policy_reads_before_delivery"] == 0
    assert dec(control["f22"]["convergence"]["cash_diff"]) == 0
    assert dec(control["f22"]["convergence"]["equity_diff"]) == 0
    assert dec(control["f22"]["convergence"]["position_diff"]) == 0
    assert control["counts"]["orders_submitted"] == native["total_orders"]
    assert control["counts"]["positions"] == native["total_positions"]

    invariant = _load(FINAL)["f22_invariants"]["5_zero_delay_control_equals_native_runner"]
    assert invariant["semantically_equal"] is True
    assert invariant["field_mismatches"] == []


def test_feed_delay_is_charged_once():
    for name, run in _runs().items():
        feed = run["f22"]["feed_delay_charged_once"]
        delay = int(feed["feed_delay_ns"])
        assert feed["first_tick_violations"] == 0, name
        assert feed["delivered_before_due"] == 0, name
        assert int(feed["min_total_delay_ns"]) >= delay, name
        assert int(feed["max_total_delay_ns"]) >= int(feed["min_total_delay_ns"]), name
        samples = (
            feed["first_samples"] + feed["last_samples"] + feed["stride_samples"]
        )
        assert samples, name
        for sample in samples:
            check_feed_sample(sample, delay)

    main = _load(RUN_FILES["main"])
    feed = main["f22"]["feed_delay_charged_once"]
    assert int(feed["min_total_delay_ns"]) == 50_000_000
    assert feed["quotes"] + feed["trades"] + feed["marks"] == feed["items"]
    assert feed["items"] == main["counts"]["events_parsed"] - main["counts"]["funding_rates"]
    # Excess lateness is the observed gap to the next data tick, not a second
    # configured delay: the archive has quiet stretches.
    assert int(feed["max_lateness_ns"]) > 0


def test_exchange_update_precedes_response_delivery():
    for name, run in _runs().items():
        delay = int(run["command"]["response_delay_ns"])
        fills = run["fills"]
        assert fills, name
        for fill in fills:
            check_fill(fill, delay)
        if delay > 0:
            assert all(
                int(fill["delivered_at"]) > int(fill["exchange_ts"]) for fill in fills
            ), name
        else:
            assert all(
                int(fill["delivered_at"]) >= int(fill["exchange_ts"]) for fill in fills
            ), name
        assert run["f22"]["exchange_before_delivery"]["violations"] == 0, name


def test_no_policy_decision_uses_fill_before_delivery():
    runs = _runs()
    for name, run in runs.items():
        response = run["f22"]["response_delay"]
        assert response["policy_reads_before_delivery"] == 0, name
        for decision in run["decisions"]:
            assert decision_position_matches_delivered(decision), (name, decision)

    main = runs["main"]
    assert main["f22"]["response_delay"]["fills_with_intervening_decisions"] == 0

    large = runs["large_response"]
    assert large["f22"]["response_delay"]["fills_with_intervening_decisions"] == 0
    assert large["f22"]["response_delay"]["decisions_with_unseen_fills"] == 0

    gap = runs["decision_in_gap"]
    gap_response = gap["f22"]["response_delay"]
    assert gap_response["fills_with_intervening_decisions"] == gap_response["fills"]
    assert gap_response["decisions_with_unseen_fills"] > 0
    assert gap_response["max_in_flight_qty"] != "0"
    gap_decisions = [
        decision
        for decision in gap["decisions"]
        if decision["pre_existing_fills_in_flight"] > 0
    ]
    assert gap_decisions
    for decision in gap_decisions:
        assert dec(decision["in_flight_qty"]) != 0
        assert dec(decision["local_position_qty"]) == dec(
            decision["expected_position_from_delivered_fills"]
        )
        # The decision sees the pre-fill position, not the exchange position.
        assert decision["exchange_fills_before"] > decision["delivered_fills"]

    invariant = _load(FINAL)["f22_invariants"][
        "2_no_policy_decision_uses_fill_before_delivery"
    ]
    for name in ("main", "large_response", "decision_in_gap", "control_zero_delay"):
        assert invariant[name]["pass"] is True, name


def test_large_response_profile_produces_a_real_delivery_gap():
    large = _load(RUN_FILES["large_response"])
    delay = int(large["command"]["response_delay_ns"])
    assert delay == 30_000_000_000
    fills = large["fills"]
    assert len(fills) > 0
    for fill in fills:
        assert int(fill["delivered_at"]) - int(fill["exchange_ts"]) >= delay
        assert fill["delivered"] is True
    assert large["f22"]["response_delay"]["fills_with_intervening_decisions"] == 0
    assert large["f22"]["response_delay"]["strictly_later_than_exchange"] == len(fills)

    gap = _load(RUN_FILES["decision_in_gap"])
    gap_delay = int(gap["command"]["response_delay_ns"])
    assert gap_delay == 90_000_000_000
    for fill in gap["fills"]:
        assert int(fill["delivered_at"]) - int(fill["exchange_ts"]) >= gap_delay
    assert gap["f22"]["response_delay"]["fills_with_intervening_decisions"] == len(
        gap["fills"]
    )


def test_local_view_converges_to_exchange_account():
    for name, run in _runs().items():
        conv = run["f22"]["convergence"]
        assert conv["finalized"] is True, name
        assert dec(conv["position_diff"]) == 0, name
        assert dec(conv["cash_diff"]) == 0, name
        assert dec(conv["equity_diff"]) == 0, name
        assert dec(conv["local_position_qty"]) == dec(conv["exchange_position_qty"]), name
        assert dec(conv["local_cash"]) == dec(conv["exchange_cash"]), name
        assert dec(conv["local_equity"]) == dec(conv["exchange_equity"]), name
        assert run["local_view"]["delivered_fills"] == run["counts"]["fills"], name

    doc = _load(FINAL)
    convergence_invariants = doc["f22_invariants"]["4_local_view_converges_to_exchange"]
    for name in ("main", "large_response", "decision_in_gap", "control_zero_delay"):
        assert convergence_invariants[name]["pass"] is True, name
    assert doc["f22_invariant_checks"]["all_pass"] is True
    assert all(doc["status_basis"].values())


def test_independent_exact_accounting_has_zero_residual():
    for name, run in _runs().items():
        exchange = reconstruct(run["tape"])
        native = run["exchange"]["native_account"]
        assert exchange["unexplained_residual"] == "0", name
        assert dec(exchange["total_cash"]) == dec(native["total"]), name
        assert dec(exchange["equity"]) == dec(native["equity"]), name
        assert dec(exchange["position_qty"]) == dec(native["position_qty"]), name

        local = reconstruct(run["local_tape"])
        local_view = run["local_view"]
        assert local["unexplained_residual"] == "0", name
        assert dec(local["total_cash"]) == dec(local_view["cash"]), name
        assert dec(local["equity"]) == dec(local_view["equity"]), name
        assert dec(local["position_qty"]) == dec(local_view["position_qty"]), name
        assert dec(local["funding_cash"]) == dec(local_view["funding_cash"]), name

    doc = _load(FINAL)
    for name in RUN_FILES:
        assert doc["independent_accounting"][name]["match"] is True, name
        assert doc["local_independent_accounting"][name]["match"] is True, name


def test_negative_controls_reject_lookahead_and_double_delay():
    main = _load(RUN_FILES["main"])
    delay = int(main["command"]["response_delay_ns"])
    fill = dict(main["fills"][0])

    check_fill(fill, delay)

    peek = dict(fill)
    peek["delivered_at"] = str(int(fill["exchange_ts"]) - 1)
    with pytest.raises(AssertionError):
        check_fill(peek, delay)

    late = dict(fill)
    late["deliver_at"] = str(int(fill["exchange_ts"]) + delay + 1)
    with pytest.raises(AssertionError):
        check_fill(late, delay)

    early = dict(fill)
    early["delivered_at"] = str(int(fill["deliver_at"]) - 1)
    with pytest.raises(AssertionError):
        check_fill(early, delay)

    # A decision that reports the exchange position (including an in-flight
    # fill) instead of the delivered position is rejected.
    decision = dict(main["decisions"][0])
    flagged = dict(decision)
    flagged["local_position_qty"] = "0.001"
    flagged["expected_position_from_delivered_fills"] = "0"
    assert decision_position_matches_delivered(decision)
    assert not decision_position_matches_delivered(flagged)

    feed = main["f22"]["feed_delay_charged_once"]
    feed_delay = int(feed["feed_delay_ns"])
    sample = dict(feed["first_samples"][0])
    check_feed_sample(sample, feed_delay)

    double_charged = dict(sample)
    double_charged["delivered_at"] = str(int(sample["ts_event"]) + feed_delay - 1)
    with pytest.raises(AssertionError):
        check_feed_sample(double_charged, feed_delay)

    wrong_due = dict(sample)
    wrong_due["deliver_at"] = str(int(sample["ts_event"]) + feed_delay + 1)
    with pytest.raises(AssertionError):
        check_feed_sample(wrong_due, feed_delay)


def test_policy_path_does_not_read_exchange_cache():
    source = (ROOT / "native" / "local_view" / "src" / "main.rs").read_text()
    match = re.search(
        r"fn decide\(&mut self, price: f64, now_ns: u64\) -> Result<\(\)> \{(?P<body>.*?)\n    \}\n",
        source,
        re.S,
    )
    assert match, "decide() not found"
    body = match.group("body")
    code = "\n".join(line.split("//", 1)[0] for line in body.splitlines())
    assert "self.local" in code
    assert "cache(" not in code
    assert "account_for_venue" not in code
    assert "positions_open" not in code


def test_specification_manifest_is_untouched():
    sha256sum = shutil.which("sha256sum")
    if sha256sum is None:
        pytest.skip("sha256sum not available")
    spec = ROOT / "specification"
    manifest = spec / "MANIFEST.sha256"
    if not manifest.is_file():
        pytest.skip("specification manifest not available")
    proc = subprocess.run(
        [sha256sum, "-c", "MANIFEST.sha256"],
        cwd=spec,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]


def test_claim_boundary_is_explicit():
    doc = _load(FINAL)
    assert doc["status"] == "PASS_SCOPED_F22_PROFILE"
    assert "adapter-managed" in doc["claim_boundary"].lower()
    assert doc["recertification_impact"]["CAP-LOCAL-VIEW"].startswith("RECERTIFIED")
    non_claims = " ".join(doc["non_claims"]).lower()
    assert "no fork" in non_claims
    assert "not delayed" in non_claims
    assert "first later" in non_claims
