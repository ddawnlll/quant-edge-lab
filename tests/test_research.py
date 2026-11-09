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
    assert first["matched_on"] == ["calendar", "trade_count"]
    assert "holding_time" not in first["matched_on"]
    assert first["draw_mapping"] == "NUMPY_PCG64_DETERMINISTIC"
