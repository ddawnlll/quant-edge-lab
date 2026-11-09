import json

from qel.research.control import matched_controls


def test_controls_are_deterministic_and_trade_matched(tmp_path):
    events = tmp_path / "events.jsonl"
    lines = []
    for i in range(30):
        lines.append(
            json.dumps(
                {
                    "kind": "Quote",
                    "ts_ns": str(1_000 + i * 100),
                    "bid": f"{100 + i * 0.1:.2f}",
                    "ask": f"{100.2 + i * 0.1:.2f}",
                }
            )
        )
    events.write_text("\n".join(lines) + "\n")
    first = matched_controls(str(events), trades=5, notional=100, seed=7, replications=3)
    second = matched_controls(str(events), trades=5, notional=100, seed=7, replications=3)
    assert first == second
    assert first["trade_count_matched"] == 5
    assert [c["trades"] for c in first["controls"]] == [5, 5, 5]
    assert first["matched_on"] == ["calendar", "trade_count", "holding_time"]
    assert first["draw_mapping"] == "HMAC_SHA256_COUNTER_V1"


import datetime
import math

from qel.research import screen
from qel.research.screen import family_decision_report


def _sine_panel(value: float, amplitude: float, phase: float, n: int = 120) -> dict:
    start = datetime.date(2026, 1, 1)
    rows = {}
    for t in range(n):
        day = (start + datetime.timedelta(days=t)).isoformat()
        rows[day] = f"{value + amplitude * math.sin(2 * math.pi * t / 20 + phase):.12f}"
    return {"BTC": rows}


def test_family_report_records_both_endpoints_and_holm_inputs():
    report = family_decision_report(
        {"C1": _sine_panel(1.0, 0.1, 0.0)},
        _sine_panel(0.5, 0.1, 0.7),
        "1000",
        "0.0001",
        "0",
        b=200,
    )
    entry = report["candidates"]["C1"]
    assert set(entry["primary"]["endpoints"]) == {"A", "B"}
    assert set(entry["sensitivity"]["endpoints"]) == {"A", "B"}
    assert len(report["holm"]["adjusted"]) == 1
    assert report["holm"]["thresholds"][0] == 0.025
    assert report["inputs"]["capital"] == "1000"
    assert report["inputs"]["sensitivity_block"] == 10
    assert report["budget"]["b_required"] == 9999
    assert report["family_note"].startswith("research family report")


def test_family_report_never_promotes(monkeypatch):
    monkeypatch.setattr(
        screen,
        "family_decision",
        lambda *args, **kwargs: {"verdict": "PROMOTE", "candidates": {}},
    )
    report = screen.family_decision_report({}, {}, "1000", "0", "0")
    assert report["statistical_verdict"] == "PROMOTE"
    assert report["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert report["boundary_reason"] == "RESEARCH_SCREEN_NEVER_PROMOTES"
