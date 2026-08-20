import asyncio
import logging
import time
from typing import Dict, Any
import aiosqlite

from app.state_machines.agent_state import AgentState, AgentStateMachine
from app.state_machines.call_state import CallState, CallStateMachine

logger = logging.getLogger("smartdialer.recovery")

class WorkerCrashRecoveryEngine:
    def __init__(self, orphan_timeout_seconds: float = 3.0):
        self.orphan_timeout_seconds = orphan_timeout_seconds

    async def reconcile_orphans(self, conn: aiosqlite.Connection) -> Dict[str, int]:
        """
        Scans DB for stuck or half-committed calls/agents resulting from worker task crashes.
        Reconciles call states and releases orphaned agents back to AVAILABLE.
        """
        now = time.time()
        cutoff = now - self.orphan_timeout_seconds
        reconciled_calls_count = 0
        reconciled_agents_count = 0

        # 1. Find stuck calls in RESERVED or INITIATED updated prior to cutoff
        async with conn.execute(
            """
            SELECT id, agent_id, borrower_id, state 
            FROM calls 
            WHERE state IN ('RESERVED', 'INITIATED') AND updated_at < ?
            """,
            (cutoff,)
        ) as cursor:
            stuck_calls = await cursor.fetchall()

        for call in stuck_calls:
            call_id = call["id"]
            agent_id = call["agent_id"]
            borrower_id = call["borrower_id"]

            logger.warning(
                f"[RecoveryEngine] Found stuck/half-committed call {call_id} in state {call['state']}. "
                "Reconciling to FAILED due to worker crash."
            )

            # Transition call to FAILED
            await CallStateMachine.transition(
                conn, call_id, CallState.FAILED,
                error_message="Worker crash reconciliation: orphan detected"
            )
            reconciled_calls_count += 1

            # Release associated agent if stuck in RESERVED or DIALING
            if agent_id:
                async with conn.execute(
                    "SELECT state FROM agents WHERE id = ?", (agent_id,)
                ) as a_cursor:
                    agent_row = await a_cursor.fetchone()
                    if agent_row and agent_row["state"] in (AgentState.RESERVED.value, AgentState.DIALING.value):
                        await AgentStateMachine.transition(conn, agent_id, AgentState.AVAILABLE)
                        reconciled_agents_count += 1

            # Requeue borrower if appropriate
            if borrower_id:
                await conn.execute(
                    "UPDATE borrowers SET status = 'PENDING', updated_at = ? WHERE id = ?",
                    (now, borrower_id)
                )

        # 2. Sweep orphaned agents in RESERVED or DIALING whose associated call is no longer active
        async with conn.execute(
            """
            SELECT id, current_call_id, state 
            FROM agents 
            WHERE state IN ('RESERVED', 'DIALING') AND updated_at < ?
            """,
            (cutoff,)
        ) as a_cursor:
            stuck_agents = await a_cursor.fetchall()

        for a_row in stuck_agents:
            agent_id = a_row["id"]
            call_id = a_row["current_call_id"]

            call_active = False
            if call_id:
                async with conn.execute(
                    "SELECT state FROM calls WHERE id = ?", (call_id,)
                ) as c_cursor:
                    c_row = await c_cursor.fetchone()
                    if c_row and c_row["state"] in ("RESERVED", "INITIATED", "RINGING", "ANSWERED", "CONNECTED"):
                        call_active = True

            if not call_active:
                logger.warning(
                    f"[RecoveryEngine] Found orphaned agent {agent_id} in state {a_row['state']}. "
                    "Releasing to AVAILABLE."
                )
                await AgentStateMachine.transition(conn, agent_id, AgentState.AVAILABLE)
                reconciled_agents_count += 1

        await conn.commit()
        return {
            "reconciled_calls": reconciled_calls_count,
            "reconciled_agents": reconciled_agents_count
        }
