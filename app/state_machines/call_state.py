import asyncio
import time
from enum import Enum
from typing import Dict, Optional, Set, Tuple
import aiosqlite

class CallState(str, Enum):
    QUEUED = "QUEUED"
    RESERVED = "RESERVED"
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    CONNECTED = "CONNECTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

STATE_RANK: Dict[CallState, int] = {
    CallState.QUEUED: 1,
    CallState.RESERVED: 2,
    CallState.INITIATED: 3,
    CallState.RINGING: 4,
    CallState.ANSWERED: 5,
    CallState.CONNECTED: 6,
    CallState.COMPLETED: 7,
    CallState.FAILED: 7,
    CallState.CANCELLED: 7,
}

TERMINAL_STATES = {CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED}

VALID_CALL_TRANSITIONS: Dict[CallState, Set[CallState]] = {
    CallState.QUEUED: {CallState.RESERVED, CallState.CANCELLED},
    CallState.RESERVED: {CallState.INITIATED, CallState.FAILED, CallState.CANCELLED},
    CallState.INITIATED: {CallState.RINGING, CallState.FAILED, CallState.CANCELLED, CallState.COMPLETED},
    CallState.RINGING: {CallState.ANSWERED, CallState.FAILED, CallState.CANCELLED, CallState.COMPLETED},
    CallState.ANSWERED: {CallState.CONNECTED, CallState.COMPLETED, CallState.FAILED},
    CallState.CONNECTED: {CallState.COMPLETED, CallState.FAILED},
    CallState.COMPLETED: set(),
    CallState.FAILED: set(),
    CallState.CANCELLED: set(),
}

class InvalidCallStateTransitionError(Exception):
    pass

class CallLockRegistry:
    _locks: Dict[str, asyncio.Lock] = {}
    _global_lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    async def get_lock(cls, call_id: str) -> asyncio.Lock:
        async with cls._global_lock:
            if call_id not in cls._locks:
                cls._locks[call_id] = asyncio.Lock()
            return cls._locks[call_id]

    @classmethod
    def reset(cls):
        cls._locks.clear()

class CallStateMachine:
    @classmethod
    async def transition(
        cls,
        conn: aiosqlite.Connection,
        call_id: str,
        target_state: CallState,
        agent_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        provider_call_id: Optional[str] = None,
        error_message: Optional[str] = None
    ) -> Tuple[dict, bool]:
        """
        Transitions call_id to target_state idempotently.
        Returns tuple: (updated_call_dict, was_transition_executed)
        """
        lock = await CallLockRegistry.get_lock(call_id)
        async with lock:
            async with conn.execute(
                """
                SELECT id, campaign_id, borrower_id, agent_id, state, provider_id, 
                       provider_call_id, error_message, created_at, updated_at, version 
                FROM calls WHERE id = ?
                """,
                (call_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    raise ValueError(f"Call {call_id} not found")
                
                curr_state = CallState(row["state"])
                curr_rank = STATE_RANK[curr_state]
                target_rank = STATE_RANK[target_state]
                curr_version = row["version"]

                # 1. Idempotency Check: Same state
                if curr_state == target_state:
                    return (dict(row), False)

                # 2. Out-of-order Check: Already reached a terminal state or higher rank
                if curr_state in TERMINAL_STATES:
                    # Ignore late events arriving after terminal completion/failure/cancellation
                    return (dict(row), False)

                if target_rank < curr_rank:
                    # Out-of-order event arriving after state already progressed
                    return (dict(row), False)

                # 3. Transition Validation
                allowed = VALID_CALL_TRANSITIONS.get(curr_state, set())
                # If target state is terminal or higher rank, allow transition for robustness
                if target_state not in allowed and target_rank <= curr_rank:
                    raise InvalidCallStateTransitionError(
                        f"Illegal call transition from {curr_state.value} to {target_state.value}"
                    )

                new_version = curr_version + 1
                now = time.time()

                updated_agent_id = agent_id or row["agent_id"]
                updated_provider_id = provider_id or row["provider_id"]
                updated_provider_call_id = provider_call_id or row["provider_call_id"]
                updated_error_message = error_message or row["error_message"]

                cursor_update = await conn.execute(
                    """
                    UPDATE calls 
                    SET state = ?, agent_id = ?, provider_id = ?, provider_call_id = ?, 
                        error_message = ?, updated_at = ?, version = ?
                    WHERE id = ? AND version = ?
                    """,
                    (
                        target_state.value,
                        updated_agent_id,
                        updated_provider_id,
                        updated_provider_call_id,
                        updated_error_message,
                        now,
                        new_version,
                        call_id,
                        curr_version
                    )
                )

                if cursor_update.rowcount != 1:
                    raise Exception(f"Concurrent update collision for call {call_id}")

                await conn.commit()
                return (
                    {
                        "id": call_id,
                        "campaign_id": row["campaign_id"],
                        "borrower_id": row["borrower_id"],
                        "agent_id": updated_agent_id,
                        "state": target_state.value,
                        "provider_id": updated_provider_id,
                        "provider_call_id": updated_provider_call_id,
                        "error_message": updated_error_message,
                        "created_at": row["created_at"],
                        "updated_at": now,
                        "version": new_version
                    },
                    True
                )
