import pytest
from app.pacing.pacing_engine import PredictivePacingEngine

def test_pacing_engine_basic_calculation():
    engine = PredictivePacingEngine(default_setup_time=10.0, default_talk_time=120.0)
    # Available=5, Busy=20, Ringing=2, Connected=18, AnswerRate=0.50
    # Expected freeing: 20 * (10/120) = 1.666
    # Expected free: 5 + 1.666 = 6.666
    # Ringing answers expected: 2 * 0.50 = 1.0
    # Net answers needed: 6.666 - 1.0 = 5.666
    # Recommended N = floor(5.666 / 0.50) = floor(11.33) = 11
    rec = engine.calculate_recommendation(
        available_agents=5,
        busy_agents=20,
        calls_ringing=2,
        calls_connected=18,
        rolling_answer_rate=0.50,
        avg_setup_time=10.0,
        avg_talk_time=120.0
    )
    assert rec.recommended_calls == 11
    assert "Expected free agents" in rec.reasoning
    assert rec.formula_steps["raw_calculated_n"] == 11

def test_pacing_engine_zero_available_agents():
    engine = PredictivePacingEngine(default_setup_time=10.0, default_talk_time=100.0)
    # Avail=0, Busy=10, Ringing=1, AnswerRate=0.20
    # Expected freeing: 10 * 0.1 = 1.0
    # Expected free: 1.0
    # Ringing answers expected: 1 * 0.20 = 0.20
    # Net answers needed: 0.80
    # Raw N = floor(0.80 / 0.20) = 4
    rec = engine.calculate_recommendation(
        available_agents=0,
        busy_agents=10,
        calls_ringing=1,
        calls_connected=9,
        rolling_answer_rate=0.20
    )
    assert rec.recommended_calls == 4
    assert rec.inputs["available_agents"] == 0

def test_pacing_engine_high_answer_rate():
    engine = PredictivePacingEngine()
    # High answer rate (80%) -> fewer calls needed to fill free agents
    rec = engine.calculate_recommendation(
        available_agents=2,
        busy_agents=0,
        calls_ringing=0,
        calls_connected=0,
        rolling_answer_rate=0.80
    )
    # Net answers needed = 2. Raw N = floor(2 / 0.80) = 2
    assert rec.recommended_calls == 2
