import logging
import math
from dataclasses import dataclass
from typing import Dict, Any

logger = logging.getLogger("smartdialer.pacing")

@dataclass
class PacingRecommendation:
    recommended_calls: int
    inputs: Dict[str, Any]
    formula_steps: Dict[str, Any]
    reasoning: str

class PredictivePacingEngine:
    def __init__(self, default_setup_time: float = 10.0, default_talk_time: float = 120.0):
        self.default_setup_time = default_setup_time
        self.default_talk_time = default_talk_time

    def calculate_recommendation(
        self,
        available_agents: int,
        busy_agents: int,
        calls_ringing: int,
        calls_connected: int,
        rolling_answer_rate: float,
        avg_setup_time: float = None,
        avg_talk_time: float = None
    ) -> PacingRecommendation:
        t_setup = avg_setup_time if (avg_setup_time and avg_setup_time > 0) else self.default_setup_time
        t_talk = avg_talk_time if (avg_talk_time and avg_talk_time > 0) else self.default_talk_time
        p_ans = max(0.01, min(1.0, rolling_answer_rate))

        # Formula Step 1: Agent freeing velocity during setup horizon
        agents_freeing = busy_agents * (t_setup / t_talk)
        expected_free_agents = available_agents + agents_freeing

        # Formula Step 2: Expected answers from current ringing calls
        expected_ringing_answers = calls_ringing * p_ans

        # Formula Step 3: Net answers needed
        net_answers_needed = max(0.0, expected_free_agents - expected_ringing_answers)

        # Formula Step 4: Recommended raw calls N
        raw_n = math.floor(net_answers_needed / p_ans) if p_ans > 0 else 0
        recommended_calls = max(0, raw_n)

        formula_steps = {
            "agents_freeing_during_setup": round(agents_freeing, 3),
            "expected_free_agents": round(expected_free_agents, 3),
            "expected_ringing_answers": round(expected_ringing_answers, 3),
            "net_answers_needed": round(net_answers_needed, 3),
            "raw_calculated_n": raw_n,
            "final_recommended_n": recommended_calls
        }

        inputs = {
            "available_agents": available_agents,
            "busy_agents": busy_agents,
            "calls_ringing": calls_ringing,
            "calls_connected": calls_connected,
            "rolling_answer_rate": round(p_ans, 4),
            "avg_setup_time": round(t_setup, 2),
            "avg_talk_time": round(t_talk, 2)
        }

        reasoning = (
            f"Input: avail={available_agents}, busy={busy_agents}, ringing={calls_ringing}, "
            f"ans_rate={p_ans:.2%}, t_setup={t_setup}s, t_talk={t_talk}s. "
            f"Expected free agents: {expected_free_agents:.2f} (avail {available_agents} + freeing {agents_freeing:.2f}). "
            f"Expected ringing answers: {expected_ringing_answers:.2f}. "
            f"Net answers needed: {net_answers_needed:.2f}. "
            f"Recommended N = floor({net_answers_needed:.2f} / {p_ans:.2f}) = {recommended_calls}."
        )

        logger.info(f"[PredictivePacingEngine] {reasoning}")

        return PacingRecommendation(
            recommended_calls=recommended_calls,
            inputs=inputs,
            formula_steps=formula_steps,
            reasoning=reasoning
        )
