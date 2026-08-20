import asyncio
import logging
import time
import uuid
from typing import Optional, Dict, Any, Tuple
import aiosqlite

from app.state_machines.agent_state import (
    AgentState,
    AgentStateMachine,
    AgentNotAvailableError,
    OptimisticLockError
)
from app.state_machines.call_state import CallState, CallStateMachine
from app.providers.base import TelecomProvider, TelecomProviderError

logger = logging.getLogger("smartdialer.progressive")

class ProgressiveDialer:
    def __init__(self, provider: TelecomProvider, max_retries: int = 3):
        self.provider = provider
        self.max_retries = max_retries

    async def execute_progressive_dial(
        self,
        conn: aiosqlite.Connection,
        campaign_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Executes strict 1:1 progressive dialing attempt:
        1. Select 1 AVAILABLE agent & 1 PENDING borrower.
        2. Reserve agent BEFORE dialing.
        3. Perform pre-dial agent check (handles agent disappearing mid-setup).
        4. Trigger telecom dial.
        5. Handle success/failure cleanup.
        """
        # Step 1: Find next PENDING borrower
        async with conn.execute(
            """
            SELECT id, name, phone_number, attempts 
            FROM borrowers 
            WHERE status = 'PENDING' AND attempts < ? 
            ORDER BY rowid ASC LIMIT 1
            """,
            (self.max_retries,)
        ) as cursor:
            borrower = await cursor.fetchone()
            if not borrower:
                logger.info("[ProgressiveDialer] No PENDING borrowers available.")
                return None

        # Step 2: Find next AVAILABLE agent
        async with conn.execute(
            "SELECT id, name, state FROM agents WHERE state = 'AVAILABLE' ORDER BY updated_at ASC LIMIT 1"
        ) as cursor:
            agent = await cursor.fetchone()
            if not agent:
                logger.info("[ProgressiveDialer] No AVAILABLE agents to allocate.")
                return None

        borrower_id = borrower["id"]
        phone_number = borrower["phone_number"]
        agent_id = agent["id"]
        call_id = f"call-prog-{uuid.uuid4().hex[:8]}"
        now = time.time()

        # Step 3: Create QUEUED Call & Reserve Agent BEFORE Dialing
        await conn.execute(
            """
            INSERT INTO calls (id, campaign_id, borrower_id, agent_id, state, created_at, updated_at, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (call_id, campaign_id, borrower_id, agent_id, CallState.QUEUED.value, now, now)
        )
        # Mark borrower IN_PROGRESS
        await conn.execute(
            "UPDATE borrowers SET status = 'IN_PROGRESS', updated_at = ? WHERE id = ?",
            (now, borrower_id)
        )
        await conn.commit()

        # Reserve Agent BEFORE Dialing
        try:
            await AgentStateMachine.reserve_agent(conn, agent_id, call_id=call_id)
            await CallStateMachine.transition(conn, call_id, CallState.RESERVED, agent_id=agent_id)
        except (AgentNotAvailableError, OptimisticLockError, ValueError) as e:
            logger.warning(f"[ProgressiveDialer] Reservation failed for agent {agent_id}: {e}")
            # Revert borrower to PENDING
            await conn.execute(
                "UPDATE borrowers SET status = 'PENDING', updated_at = ? WHERE id = ?",
                (time.time(), borrower_id)
            )
            await CallStateMachine.transition(conn, call_id, CallState.CANCELLED, error_message=str(e))
            await conn.commit()
            return None

        # Step 4: Pre-dial Agent Disappearance Verification
        async with conn.execute(
            "SELECT state FROM agents WHERE id = ?", (agent_id,)
        ) as cursor:
            check_agent = await cursor.fetchone()
            if not check_agent or check_agent["state"] != AgentState.RESERVED.value:
                logger.warning(
                    f"[ProgressiveDialer] Agent {agent_id} state changed to {check_agent['state'] if check_agent else 'NONE'} "
                    "during setup. Aborting call."
                )
                await CallStateMachine.transition(
                    conn, call_id, CallState.CANCELLED, error_message="Agent disappeared/state mutated before dial"
                )
                # Requeue borrower
                await conn.execute(
                    "UPDATE borrowers SET status = 'PENDING', updated_at = ? WHERE id = ?",
                    (time.time(), borrower_id)
                )
                await conn.commit()
                return None

        # Step 5: Execute Dialing via Provider
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
            return {
                "call_id": call_id,
                "agent_id": agent_id,
                "borrower_id": borrower_id,
                "provider_call_id": provider_call_id,
                "status": "INITIATED"
            }

        except (TelecomProviderError, Exception) as err:
            logger.error(f"[ProgressiveDialer] Call dial failed for {call_id}: {err}")
            # Step 6: Failure Handling - Release Agent & Requeue/Drop Borrower
            await CallStateMachine.transition(conn, call_id, CallState.FAILED, error_message=str(err))
            
            # Release agent back to AVAILABLE if still RESERVED or DIALING
            try:
                async with conn.execute("SELECT state FROM agents WHERE id = ?", (agent_id,)) as cursor:
                    a_row = await cursor.fetchone()
                    if a_row and a_row["state"] in {AgentState.RESERVED.value, AgentState.DIALING.value}:
                        await AgentStateMachine.transition(conn, agent_id, AgentState.AVAILABLE)
            except Exception as release_err:
                logger.error(f"Failed to release agent {agent_id}: {release_err}")

            # Requeue or drop borrower based on retry count
            attempts = borrower["attempts"] + 1
            new_borrower_status = "PENDING" if attempts < self.max_retries else "FAILED"
            await conn.execute(
                "UPDATE borrowers SET attempts = ?, status = ?, updated_at = ? WHERE id = ?",
                (attempts, new_borrower_status, time.time(), borrower_id)
            )
            await conn.commit()
            return None
