from qel.conformance import Harness, summarize
from qel.cli import mutations


def test_all_fixture_scenarios_pass():
    result = summarize(Harness().run_all())
    assert result["failed"] == 0, result
    assert result["total_scenarios"] == 44


def test_acceptance_fixtures_pass():
    harness = Harness()
    for fixture_id in ["F01", "F02", "F03", "F04", "F08", "F09", "F10", "F13", "F15", "F23", "F31"]:
        for result in harness.run_fixture(fixture_id):
            assert result["status"] == "PASS", (fixture_id, result["checks"])


def test_mutations_are_caught():
    report = mutations()
    assert report["all_caught"], report


def test_expected_values_do_not_influence_derived_metrics():
    harness = Harness()
    baseline = {id(record): harness.run(record)["metrics"] for record in harness.records}
    for record in harness.records:
        record["expected"] = []
    independent = {id(record): harness.run(record)["metrics"] for record in harness.records}
    assert baseline == independent
