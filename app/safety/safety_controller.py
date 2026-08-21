import logging
import math
from enum import Enum
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
import aiosqlite

from app.progressive.progressive_dialer import ProgressiveDialer

logger = logging.getLogger("smartdialer.safety")

class SafetyAction(str, Enum):
    APPROVE = "APPROVE"
    REDUCE = "REDUCE"
    REJECT = "REJECT"
    FALLBACK_PROGRESSIVE = "FALLBACK_PROGRESSIVE"

@dataclass
class SafetyDecision:
    action: SafetyAction
    approved_calls: int
    raw_recommended: int
    max_allowed: int
    abandon_rate: float
    target_abandon_rate: float
    reason: str

class SafetyController:
    def __init__(
        self,
        target_abandon_rate: float = 0.03,
        target_failure_rate: float = 0.50,
        progressive_dialer: Optional[ProgressiveDialer] = None
    ):
        self.target_abandon_rate = target_abandon_rate
        self.target_failure_rate = target_failure_rate  # Provider health threshold (technical failures)
        self.progressive_dialer = progressive_dialer

    def evaluate(
        self,
        raw_recommendation: int,
        available_agents: int,
        predicted_answer_rate: float,
        recent_abandon_rate: float,
        recent_failure_rate: float = 0.0,
        current_ringing_calls: int = 0
    ) -> SafetyDecision:
        """
        Independently checks and caps raw recommended calls N from Predictive Pacing Engine.
        Reacts to TWO distinct risk signals:
        - recent_abandon_rate: borrower-side compliance risk (answered, no agent free)
        - recent_failure_rate: provider-side technical risk (dial failures / no-answers spiking,
          which can indicate a broken telecom provider rather than borrower abandonment)
        """
        # 1. Check abandon rate breach (compliance risk)
        if recent_abandon_rate > self.target_abandon_rate:
            reason = (
                f"Abandon rate {recent_abandon_rate:.2%} exceeds target {self.target_abandon_rate:.2%}. "
                "Triggering FALLBACK_PROGRESSIVE mode."
            )
            logger.warning(f"[SafetyController] {reason}")
            return SafetyDecision(
                action=SafetyAction.FALLBACK_PROGRESSIVE,
                approved_calls=available_agents, # 1:1 progressive fallback capacity
                raw_recommended=raw_recommendation,
                max_allowed=available_agents,
                abandon_rate=recent_abandon_rate,
                target_abandon_rate=self.target_abandon_rate,
                reason=reason
            )

        # 2. Check technical failure rate breach (provider health risk)
        if recent_failure_rate > self.target_failure_rate:
            reason = (
                f"Technical failure rate {recent_failure_rate:.2%} exceeds threshold {self.target_failure_rate:.2%}. "
                "Likely provider outage -- triggering FALLBACK_PROGRESSIVE mode."
            )
            logger.warning(f"[SafetyController] {reason}")
            return SafetyDecision(
                action=SafetyAction.FALLBACK_PROGRESSIVE,
                approved_calls=available_agents,
                raw_recommended=raw_recommendation,
                max_allowed=available_agents,
                abandon_rate=recent_abandon_rate,
                target_abandon_rate=self.target_abandon_rate,
                reason=reason
            )

        # 3. Compute dynamic safety buffer based on answer rate
        buffer = math.ceil(available_agents * (1.0 - max(0.05, min(0.95, predicted_answer_rate))))
        max_allowed = max(0, available_agents + buffer - current_ringing_calls)

        if available_agents <= 0 and raw_recommendation > 0:
            max_allowed = 0

        if raw_recommendation <= 0:
            return SafetyDecision(
                action=SafetyAction.REJECT,
                approved_calls=0,
                raw_recommended=raw_recommendation,
                max_allowed=max_allowed,
                abandon_rate=recent_abandon_rate,
                target_abandon_rate=self.target_abandon_rate,
                reason="Raw pacing recommendation is 0."
            )

        if raw_recommendation > max_allowed:
            if max_allowed <= 0:
                return SafetyDecision(
                    action=SafetyAction.REJECT,
                    approved_calls=0,
                    raw_recommended=raw_recommendation,
                    max_allowed=max_allowed,
                    abandon_rate=recent_abandon_rate,
                    target_abandon_rate=self.target_abandon_rate,
                    reason=f"Raw recommendation {raw_recommendation} exceeds max safe limit {max_allowed} (0 allowed)."
                )
            
            return SafetyDecision(
                action=SafetyAction.REDUCE,
                approved_calls=max_allowed,
                raw_recommended=raw_recommendation,
                max_allowed=max_allowed,
                abandon_rate=recent_abandon_rate,
                target_abandon_rate=self.target_abandon_rate,
                reason=f"Raw recommendation {raw_recommendation} reduced to max allowed cap {max_allowed}."
            )

        return SafetyDecision(
            action=SafetyAction.APPROVE,
            approved_calls=raw_recommendation,
            raw_recommended=raw_recommendation,
            max_allowed=max_allowed,
            abandon_rate=recent_abandon_rate,
            target_abandon_rate=self.target_abandon_rate,
            reason=f"Recommendation {raw_recommendation} approved within max cap {max_allowed}."
        )

    async def handle_fallback_progressive(
        self,
        conn: aiosqlite.Connection,
        campaign_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Delegates directly to ProgressiveDialer for 1:1 call execution.
        """
        if not self.progressive_dialer:
            raise ValueError("ProgressiveDialer instance not configured in SafetyController.")
        return await self.progressive_dialer.execute_progressive_dial(conn, campaign_id)
