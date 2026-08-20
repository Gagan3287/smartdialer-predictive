import asyncio
import logging
import time
import uuid
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
    def __init__(self, provider: TelecomProvider, max_retries: int = 3):
        self.provider = provider
        self.max_retries = max_retries

    async def allocate_and_dial(
        self,
        conn: aiosqlite.Connection,
        campaign_id: str,
        target_count: int
    ) -> List[Dict[str, Any]]:
        """
        Allocates up to target_count calls:
        1. Selects target_count AVAILABLE agents and PENDING borrowers.
        2. Reserves agents BEFORE dispatching dials.
        3. Initiates calls via TelecomProvider.
        """
        if target_count <= 0:
            return []

        # 1. Fetch available agents
        async with conn.execute(
            "SELECT id, name FROM agents WHERE state = 'AVAILABLE' ORDER BY updated_at ASC LIMIT ?",
            (target_count,)
        ) as cursor:
            agents = await cursor.fetchall()

        if not agents:
            logger.info("[CallAllocator] No AVAILABLE agents for predictive allocation.")
            return []

        actual_batch_size = len(agents)

        # 2. Fetch pending borrowers
        async with conn.execute(
            """
            SELECT id, name, phone_number, attempts 
            FROM borrowers 
            WHERE status = 'PENDING' AND attempts < ? 
            ORDER BY rowid ASC LIMIT ?
            """,
            (self.max_retries, actual_batch_size)
        ) as cursor:
            borrowers = await cursor.fetchall()

        if not borrowers:
            logger.info("[CallAllocator] No PENDING borrowers for predictive allocation.")
            return []

        allocations = list(zip(agents, borrowers))
        results = []

        for agent, borrower in allocations:
            agent_id = agent["id"]
            borrower_id = borrower["id"]
            phone_number = borrower["phone_number"]
            call_id = f"call-pred-{uuid.uuid4().hex[:8]}"
            now = time.time()

            # Insert QUEUED call & mark borrower IN_PROGRESS
            await conn.execute(
                """
                INSERT INTO calls (id, campaign_id, borrower_id, agent_id, state, created_at, updated_at, version)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (call_id, campaign_id, borrower_id, agent_id, CallState.QUEUED.value, now, now)
            )
            await conn.execute(
                "UPDATE borrowers SET status = 'IN_PROGRESS', updated_at = ? WHERE id = ?",
                (now, borrower_id)
            )
            await conn.commit()

            # Reserve Agent
            try:
                await AgentStateMachine.reserve_agent(conn, agent_id, call_id=call_id)
                await CallStateMachine.transition(conn, call_id, CallState.RESERVED, agent_id=agent_id)
            except (AgentNotAvailableError, OptimisticLockError, ValueError) as e:
                logger.warning(f"[CallAllocator] Failed reserving agent {agent_id}: {e}")
                await conn.execute(
                    "UPDATE borrowers SET status = 'PENDING', updated_at = ? WHERE id = ?",
                    (time.time(), borrower_id)
                )
                await CallStateMachine.transition(conn, call_id, CallState.CANCELLED, error_message=str(e))
                await conn.commit()
                continue

            # Execute Dial
            try:
                await AgentStateMachine.transition(conn, agent_id, AgentState.DIALING, call_id=call_id)
                await CallStateMachine.transition(conn, call_id, CallState.INITIATED, agent_id=agent_id)

                telecom_res = await self.provider.dial(call_id, phone_number)
                provider_call_id = telecom_res.get("provider_call_id")
                provider_id = telecom_res.get("provider_id")

                await CallStateMachine.transition(
                    conn, call_id, CallState.RINGING,
                    agent_id=agent_id, provider_id=provider_id, provider_call_id=provider_call_id
                )
                results.append({
                    "call_id": call_id,
                    "agent_id": agent_id,
                    "borrower_id": borrower_id,
                    "provider_call_id": provider_call_id
                })
            except (TelecomProviderError, Exception) as err:
                logger.error(f"[CallAllocator] Dial failed for {call_id}: {err}")
                await CallStateMachine.transition(conn, call_id, CallState.FAILED, error_message=str(err))
                # Release agent
                try:
                    await AgentStateMachine.transition(conn, agent_id, AgentState.AVAILABLE)
                except Exception:
                    pass
                # Requeue borrower
                attempts = borrower["attempts"] + 1
                new_status = "PENDING" if attempts < self.max_retries else "FAILED"
                await conn.execute(
                    "UPDATE borrowers SET attempts = ?, status = ?, updated_at = ? WHERE id = ?",
                    (attempts, new_status, time.time(), borrower_id)
                )
                await conn.commit()

        return results
