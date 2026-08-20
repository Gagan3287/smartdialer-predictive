import pytest
from app.safety.safety_controller import SafetyController, SafetyAction

def test_safety_controller_approve():
    controller = SafetyController(target_abandon_rate=0.03)
    decision = controller.evaluate(
        raw_recommendation=3,
        available_agents=5,
        predicted_answer_rate=0.50,
        recent_abandon_rate=0.01,
        current_ringing_calls=0
    )
    assert decision.action == SafetyAction.APPROVE
    assert decision.approved_calls == 3

def test_safety_controller_reduce():
    controller = SafetyController(target_abandon_rate=0.03)
    # Available 2, high answer rate (0.80) -> buffer ceil(2 * 0.20) = 1 -> max allowed 3
    # If raw recommendation is 10, should reduce to max allowed (3)
    decision = controller.evaluate(
        raw_recommendation=10,
        available_agents=2,
        predicted_answer_rate=0.80,
        recent_abandon_rate=0.01,
        current_ringing_calls=0
    )
    assert decision.action == SafetyAction.REDUCE
    assert decision.approved_calls <= 3

def test_safety_controller_fallback_progressive():
    controller = SafetyController(target_abandon_rate=0.03)
    # Recent abandon rate 0.05 > 0.03 target abandon rate -> trigger FALLBACK_PROGRESSIVE
    decision = controller.evaluate(
        raw_recommendation=5,
        available_agents=4,
        predicted_answer_rate=0.50,
        recent_abandon_rate=0.05,
        current_ringing_calls=1
    )
    assert decision.action == SafetyAction.FALLBACK_PROGRESSIVE
    assert decision.approved_calls == 4
