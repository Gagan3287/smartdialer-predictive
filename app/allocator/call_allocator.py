import asyncio
import logging
import time
import uuid
import random
from typing import List, Dict, Any
import aiosqlite

from app.state_machines.agent_state import (
    AgentState,
    AgentStateMachine,
    AgentNotAvailableError,
    OptimisticLockError
)
from app.state_machines.call_state import CallState, CallStateMachine
from app.providers.base import TelecomProvider, TelecomProviderError

logger = logging.getLogger("smartdialer.allocator")

class CallAllocator:
    """
    Predictive-mode allocator. Dials up to target_count borrowers WITHOUT
    pre-reserving an agent for each -- this is the actual predictive over-dial
    mechanism: more calls can be dialed than there are agents. An agent is
    reserved only if/when the borrower actually answers. If no agent is free
    at that moment, the call becomes a genuine ABANDONED call (answered,
    no agent) rather than being silently prevented from happening.
    """

    def __init__(self, provider: TelecomProvider, max_retries: int = 3, simulated_answer_rate: float = None):
        self.provider = provider
        self.max_retries = max_retries
        self.simulated_answer_rate = simulated_answer_rate

    async def _try_reserve_any_available_agent(self, conn: aiosqlite.Connection, call_id: str):
        """
        Attempts to reserve any currently AVAILABLE agent for a call that has
        already been answered. Returns the reserved agent_id, or None if no
        agent could be reserved.
        """
        async with conn.execute(
            "SELECT id FROM agents WHERE state = 'AVAILABLE' ORDER BY updated_at ASC LIMIT 10"
        ) as cursor:
            candidates = await cursor.fetchall()

        for row in candidates:
            agent_id = row["id"]
            try:
                await AgentStateMachine.reserve_agent(conn, agent_id, call_id=call_id)
                return agent_id
            except (AgentNotAvailableError, OptimisticLockError):
                continue
        return None

    async def allocate_and_dial(
        self,
        conn: aiosqlite.Connection,
        campaign_id: str,
        target_count: int
    ) -> List[Dict[str, Any]]:
        if target_count <= 0:
            return []

        # Fetch PENDING borrowers -- NOT limited to available agent count.
        # This is what allows predictive mode to genuinely over-dial.
        async with conn.execute(
            """
            SELECT id, name, phone_number, attempts 
            FROM borrowers 
            WHERE status = 'PENDING' AND attempts < ? 
            ORDER BY rowid ASC LIMIT ?
            """,
            (self.max_retries, target_count)
        ) as cursor:
            borrowers = await cursor.fetchall()

        if not borrowers:
            logger.info("[CallAllocator] No PENDING borrowers for predictive allocation.")
            return []

        results = []

        for borrower in borrowers:
            borrower_id = borrower["id"]
            phone_number = borrower["phone_number"]
            call_id = f"call-pred-{uuid.uuid4().hex[:8]}"
            now = time.time()

            # Insert QUEUED call with NO agent assigned yet.
            await conn.execute(
                """
                INSERT INTO calls (id, campaign_id, borrower_id, agent_id, state, created_at, updated_at, version)
                VALUES (?, ?, ?, NULL, ?, ?, ?, 1)
                """,
                (call_id, campaign_id, borrower_id, CallState.QUEUED.value, now, now)
            )
            await conn.execute(
                "UPDATE borrowers SET status = 'IN_PROGRESS', updated_at = ? WHERE id = ?",
                (now, borrower_id)
            )
            await conn.commit()

            try:
                await CallStateMachine.transition(conn, call_id, CallState.INITIATED)

                telecom_res = await self.provider.dial(call_id, phone_number)
                provider_call_id = telecom_res.get("provider_call_id")
                provider_id = telecom_res.get("provider_id")

                await CallStateMachine.transition(
                    conn, call_id, CallState.RINGING,
                    provider_id=provider_id, provider_call_id=provider_call_id
                )

                # Simulate whether the borrower answers, based on configured rate.
                if self.simulated_answer_rate is not None:
                    borrower_answered = random.random() < self.simulated_answer_rate

                    if not borrower_answered:
                        # NOTE the "NO_ANSWER:" prefix -- this distinguishes an
                        # ordinary simulated no-pickup from a real technical/provider
                        # failure below. Both land in CallState.FAILED, but only
                        # true technical failures should count toward the Safety
                        # Controller's provider-health signal (see campaign_manager.py).
                        await CallStateMachine.transition(
                            conn, call_id, CallState.FAILED, error_message="NO_ANSWER: simulated no pickup"
                        )
                        attempts = borrower["attempts"] + 1
                        new_status = "PENDING" if attempts < self.max_retries else "FAILED"
                        await conn.execute(
                            "UPDATE borrowers SET attempts = ?, status = ?, updated_at = ? WHERE id = ?",
                            (attempts, new_status, time.time(), borrower_id)
                        )
                        await conn.commit()
                        continue

                    # Borrower answered -- NOW attempt to reserve an agent.
                    agent_id = await self._try_reserve_any_available_agent(conn, call_id)

                    if agent_id is None:
                        # TRUE abandoned call: answered, but no agent was free.
                        await CallStateMachine.transition(
                            conn, call_id, CallState.ABANDONED,
                            error_message="Borrower answered but no agent was available"
                        )
                        await conn.execute(
                            "UPDATE borrowers SET status = 'PENDING', updated_at = ? WHERE id = ?",
                            (time.time(), borrower_id)
                        )
                        logger.warning(f"[CallAllocator] TRUE ABANDONMENT: call {call_id} answered with no agent free.")
                        await conn.commit()
                        continue

                    # Agent secured -- complete the call normally.
                    await AgentStateMachine.transition(conn, agent_id, AgentState.DIALING, call_id=call_id)
                    await CallStateMachine.transition(conn, call_id, CallState.ANSWERED, agent_id=agent_id)
                    await CallStateMachine.transition(conn, call_id, CallState.CONNECTED, agent_id=agent_id)
                    await CallStateMachine.transition(conn, call_id, CallState.COMPLETED, agent_id=agent_id)
                    await AgentStateMachine.transition(conn, agent_id, AgentState.WRAP_UP)
                    await AgentStateMachine.transition(conn, agent_id, AgentState.AVAILABLE)
                    await conn.commit()

                results.append({
                    "call_id": call_id,
                    "borrower_id": borrower_id,
                    "provider_call_id": provider_call_id
                })

            except (TelecomProviderError, Exception) as err:
                # A real technical/provider failure -- error_message here does NOT
                # carry the "NO_ANSWER:" prefix, so it correctly counts toward the
                # Safety Controller's technical failure rate (provider health signal).
                logger.error(f"[CallAllocator] Dial failed for {call_id}: {err}")
                await CallStateMachine.transition(conn, call_id, CallState.FAILED, error_message=str(err))
                attempts = borrower["attempts"] + 1
                new_status = "PENDING" if attempts < self.max_retries else "FAILED"
                await conn.execute(
                    "UPDATE borrowers SET attempts = ?, status = ?, updated_at = ? WHERE id = ?",
                    (attempts, new_status, time.time(), borrower_id)
                )
                await conn.commit()

        return results