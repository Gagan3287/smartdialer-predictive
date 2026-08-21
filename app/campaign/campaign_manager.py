import asyncio
import logging
import time
import json
from typing import Dict, Any, List, Optional
import aiosqlite

from app.pacing.pacing_engine import PredictivePacingEngine, PacingRecommendation
from app.safety.safety_controller import SafetyController, SafetyAction, SafetyDecision
from app.allocator.call_allocator import CallAllocator
from app.progressive.progressive_dialer import ProgressiveDialer
from app.providers.base import TelecomProvider

logger = logging.getLogger("smartdialer.campaign")

class CampaignManager:
    def __init__(
        self,
        pacing_engine: PredictivePacingEngine,
        safety_controller: SafetyController,
        allocator: CallAllocator,
        progressive_dialer: ProgressiveDialer
    ):
        self.pacing_engine = pacing_engine
        self.safety_controller = safety_controller
        self.allocator = allocator
        self.progressive_dialer = progressive_dialer

    async def get_metrics(self, conn: aiosqlite.Connection) -> Dict[str, Any]:
        """
        Gathers live operational metrics for pacing & safety decisions.
        """
        # Agent counts
        async with conn.execute("SELECT state, COUNT(*) as cnt FROM agents GROUP BY state") as cursor:
            agent_rows = await cursor.fetchall()
            agent_counts = {row["state"]: row["cnt"] for row in agent_rows}

        available_agents = agent_counts.get("AVAILABLE", 0)
        busy_agents = agent_counts.get("DIALING", 0) + agent_counts.get("CONNECTED", 0) + agent_counts.get("WRAP_UP", 0)

        # Call counts
        async with conn.execute("SELECT state, COUNT(*) as cnt FROM calls GROUP BY state") as cursor:
            call_rows = await cursor.fetchall()
            call_counts = {row["state"]: row["cnt"] for row in call_rows}

        calls_ringing = call_counts.get("RINGING", 0) + call_counts.get("INITIATED", 0)
        calls_connected = call_counts.get("CONNECTED", 0)

        # Rolling answer rate, abandon rate, and failure rate over recent calls
        async with conn.execute(
            "SELECT state, error_message FROM calls WHERE state IN ('COMPLETED', 'FAILED', 'CANCELLED', 'ABANDONED') ORDER BY updated_at DESC LIMIT 50"
        ) as cursor:
            recent_calls = await cursor.fetchall()

        if recent_calls:
            total_finished = len(recent_calls)
            answered_cnt = sum(1 for r in recent_calls if r["state"] == "COMPLETED")
            # Abandon rate = compliance-relevant abandonment only: borrower
            # answered but no agent was free. NOT the same as a plain no-answer
            # (FAILED), which is normal call outcome noise, not a compliance risk.
            abandoned_cnt = sum(1 for r in recent_calls if r["state"] == "ABANDONED")
            # Technical failure rate = TRUE provider/technical errors only.
            # A plain "no answer" (tagged NO_ANSWER: by the allocator) is normal
            # call outcome noise, not a provider health signal -- conflating the
            # two would make the Safety Controller panic-trigger FALLBACK_PROGRESSIVE
            # at any low answer-rate scenario even when the provider is healthy.
            failed_cnt = sum(
                1 for r in recent_calls
                if r["state"] == "FAILED" and not (r["error_message"] or "").startswith("NO_ANSWER:")
            )
            rolling_answer_rate = answered_cnt / total_finished
            rolling_abandon_rate = abandoned_cnt / total_finished
            rolling_failure_rate = failed_cnt / total_finished
        else:
            rolling_answer_rate = 0.40  # Default initial historical assumption
            rolling_abandon_rate = 0.0
            rolling_failure_rate = 0.0

        return {
            "available_agents": available_agents,
            "busy_agents": busy_agents,
            "calls_ringing": calls_ringing,
            "calls_connected": calls_connected,
            "rolling_answer_rate": rolling_answer_rate,
            "rolling_abandon_rate": rolling_abandon_rate,
            "rolling_failure_rate": rolling_failure_rate
        }

    async def execute_pacing_tick(
        self,
        conn: aiosqlite.Connection,
        campaign_id: str,
        answer_rate_override: float = None
    ) -> Dict[str, Any]:
        """
        Executes one full iteration of the SmartDialer pipeline:
        1. Metrics Gathering
        2. Predictive Pacing Calculation
        3. Safety Controller Override Check
        4. Allocation (Predictive Batch or Progressive 1:1)
        5. Audit Logging

        answer_rate_override: when provided (e.g. by a scenario simulation),
        overrides the DB-derived rolling answer rate for this tick. Useful when
        there isn't yet enough real call history to compute a meaningful rolling
        average, or when simulating a known/configured answer rate scenario.
        """
        metrics = await self.get_metrics(conn)

        if answer_rate_override is not None:
            metrics["rolling_answer_rate"] = answer_rate_override

        # Step 1: Predictive Pacing Calculation
        pacing_rec = self.pacing_engine.calculate_recommendation(
            available_agents=metrics["available_agents"],
            busy_agents=metrics["busy_agents"],
            calls_ringing=metrics["calls_ringing"],
            calls_connected=metrics["calls_connected"],
            rolling_answer_rate=metrics["rolling_answer_rate"]
        )

        # Step 2: Safety Controller Inspection
        safety_decision = self.safety_controller.evaluate(
            raw_recommendation=pacing_rec.recommended_calls,
            available_agents=metrics["available_agents"],
            predicted_answer_rate=metrics["rolling_answer_rate"],
            recent_abandon_rate=metrics["rolling_abandon_rate"],
            recent_failure_rate=metrics["rolling_failure_rate"],
            current_ringing_calls=metrics["calls_ringing"]
        )

        # Step 3: Execution based on Safety Decision
        allocated_calls = []

        if safety_decision.action == SafetyAction.FALLBACK_PROGRESSIVE:
            logger.warning("[CampaignManager] Safety Controller triggered FALLBACK_PROGRESSIVE")
            prog_res = await self.progressive_dialer.execute_progressive_dial(conn, campaign_id)
            if prog_res:
                allocated_calls.append(prog_res)
        elif safety_decision.action in (SafetyAction.APPROVE, SafetyAction.REDUCE):
            if safety_decision.approved_calls > 0:
                allocated_calls = await self.allocator.allocate_and_dial(
                    conn=conn,
                    campaign_id=campaign_id,
                    target_count=safety_decision.approved_calls
                )

        # Step 4: Audit Logging
        now = time.time()
        audit_details = json.dumps({
            "pacing_inputs": pacing_rec.inputs,
            "pacing_formula_steps": pacing_rec.formula_steps,
            "pacing_reasoning": pacing_rec.reasoning,
            "safety_action": safety_decision.action.value,
            "safety_approved_calls": safety_decision.approved_calls,
            "safety_reason": safety_decision.reason,
            "allocated_count": len(allocated_calls)
        })

        await conn.execute(
            """
            INSERT INTO audit_logs (timestamp, entity_type, entity_id, event_name, details)
            VALUES (?, ?, ?, ?, ?)
            """,
            (now, "CAMPAIGN", campaign_id, "PACING_TICK", audit_details)
        )
        await conn.commit()

        return {
            "campaign_id": campaign_id,
            "metrics": metrics,
            "pacing": {
                "recommended_calls": pacing_rec.recommended_calls,
                "reasoning": pacing_rec.reasoning
            },
            "safety": {
                "action": safety_decision.action.value,
                "approved_calls": safety_decision.approved_calls,
                "reason": safety_decision.reason
            },
            "allocated_calls_count": len(allocated_calls)
        }