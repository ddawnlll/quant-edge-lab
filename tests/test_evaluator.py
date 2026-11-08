import pytest

from qel.evaluator.core import SealedError, SealedRegistry, f25_scenario, f26_scenario


def test_f25_burn_before_read(tmp_path):
    out = f25_scenario(str(tmp_path / "sealed"))
    assert out["consumption_count"] == 1
    assert out["alpha_spent"] == "0.025"
    assert out["retry_alpha_spend"] == 0
    assert out["new_candidate_denied"] is True
    assert out["receipt_anchored_before_read"] is True


def test_f26_fencing_and_restore(tmp_path):
    out = f26_scenario(str(tmp_path / "sealed"))
    assert out["stale_result_committed"] is False
    assert out["duplicate_trial_reserved"] is False
    assert out["sealed_state"] == "WAITING_FRESH_OOS"
    assert out["alpha_counter_decreased"] is False


def test_signed_decision_verifies(tmp_path):
    reg = SealedRegistry(str(tmp_path / "sealed"))
    reg.freeze_cohort("COHORT-0", "PROG-LAB", 0, ["a" * 64], "b" * 64, "c" * 64, "d" * 64, ["e" * 64])
    reg.spend_consumption("COHORT-0", acknowledged=True)
    decision = reg.decide("COHORT-0", [0.002, 0.001, 0.003, 0.002, 0.001], [0.0, 0.001, -0.001, 0.0, 0.0], capital=1000, economic_minimum=0.0001, b=64)
    assert decision["verdict"] in ("EDGE", "NO_EDGE", "INSUFFICIENT_EVIDENCE")
    assert reg.verify(decision)
    tampered = {**decision, "verdict": "EDGE" if decision["verdict"] != "EDGE" else "NO_EDGE"}
    assert not reg.verify(tampered)


def test_no_consumption_no_decision(tmp_path):
    reg = SealedRegistry(str(tmp_path / "sealed"))
    reg.freeze_cohort("COHORT-0", "PROG-LAB", 0, ["a" * 64], "b" * 64, "c" * 64, "d" * 64, ["e" * 64])
    with pytest.raises(SealedError):
        reg.decide("COHORT-0", [0.1, 0.1, 0.1], [0, 0, 0], capital=1000, economic_minimum=0.001)
    assert not reg.key_path.stat().st_mode & 0o077
