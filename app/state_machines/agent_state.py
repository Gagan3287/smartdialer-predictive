import asyncio
import time
from enum import Enum
from typing import Dict, Optional, Set
import aiosqlite

class AgentState(str, Enum):
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    DIALING = "DIALING"
    CONNECTED = "CONNECTED"
    WRAP_UP = "WRAP_UP"
    PAUSED = "PAUSED"

VALID_AGENT_TRANSITIONS: Dict[AgentState, Set[AgentState]] = {
    AgentState.OFFLINE: {AgentState.AVAILABLE, AgentState.PAUSED},
    AgentState.AVAILABLE: {AgentState.RESERVED, AgentState.PAUSED, AgentState.OFFLINE},
    AgentState.RESERVED: {AgentState.DIALING, AgentState.AVAILABLE, AgentState.PAUSED, AgentState.OFFLINE},
    AgentState.DIALING: {AgentState.CONNECTED, AgentState.AVAILABLE, AgentState.WRAP_UP, AgentState.PAUSED, AgentState.OFFLINE},
    AgentState.CONNECTED: {AgentState.WRAP_UP, AgentState.AVAILABLE},
    AgentState.WRAP_UP: {AgentState.AVAILABLE, AgentState.PAUSED, AgentState.OFFLINE},
    AgentState.PAUSED: {AgentState.AVAILABLE, AgentState.OFFLINE},
}

class InvalidStateTransitionError(Exception):
    pass

class AgentNotAvailableError(Exception):
    pass

class OptimisticLockError(Exception):
    pass

class AgentLockRegistry:
    _locks: Dict[str, asyncio.Lock] = {}
    _global_lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    async def get_lock(cls, agent_id: str) -> asyncio.Lock:
        async with cls._global_lock:
            if agent_id not in cls._locks:
                cls._locks[agent_id] = asyncio.Lock()
            return cls._locks[agent_id]

    @classmethod
    def reset(cls):
        cls._locks.clear()

class AgentStateMachine:
    @staticmethod
    def validate_transition(current_state: AgentState, target_state: AgentState) -> None:
        if current_state == target_state:
            return
        allowed = VALID_AGENT_TRANSITIONS.get(current_state, set())
        if target_state not in allowed:
            raise InvalidStateTransitionError(
                f"Illegal agent state transition from {current_state.value} to {target_state.value}"
            )

    @classmethod
    async def transition(
        cls,
        conn: aiosqlite.Connection,
        agent_id: str,
        target_state: AgentState,
        expected_version: Optional[int] = None,
        call_id: Optional[str] = None
    ) -> dict:
        lock = await AgentLockRegistry.get_lock(agent_id)
        async with lock:
            async with conn.execute(
                "SELECT id, name, state, version, current_call_id FROM agents WHERE id = ?", (agent_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    raise ValueError(f"Agent {agent_id} not found")
                
                curr_state = AgentState(row["state"])
                curr_version = row["version"]

                if expected_version is not None and curr_version != expected_version:
                    raise OptimisticLockError(
                        f"Agent {agent_id} version mismatch: expected {expected_version}, found {curr_version}"
                    )

                cls.validate_transition(curr_state, target_state)

                new_version = curr_version + 1
                now = time.time()
                new_call_id = call_id if target_state in {AgentState.RESERVED, AgentState.DIALING, AgentState.CONNECTED} else (
                    None if target_state in {AgentState.AVAILABLE, AgentState.OFFLINE, AgentState.PAUSED} else row["current_call_id"]
                )

                cursor_update = await conn.execute(
                    """
                    UPDATE agents 
                    SET state = ?, version = ?, current_call_id = ?, updated_at = ?
                    WHERE id = ? AND version = ?
                    """,
                    (target_state.value, new_version, new_call_id, now, agent_id, curr_version)
                )

                if cursor_update.rowcount != 1:
                    raise OptimisticLockError(f"Concurrent update failed for agent {agent_id}")

                await conn.commit()
                return {
                    "id": agent_id,
                    "name": row["name"],
                    "state": target_state.value,
                    "version": new_version,
                    "current_call_id": new_call_id,
                    "updated_at": now
                }

    @classmethod
    async def reserve_agent(
        cls,
        conn: aiosqlite.Connection,
        agent_id: str,
        call_id: str
    ) -> dict:
        lock = await AgentLockRegistry.get_lock(agent_id)
        async with lock:
            async with conn.execute(
                "SELECT state, version FROM agents WHERE id = ?", (agent_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    raise ValueError(f"Agent {agent_id} not found")
                
                curr_state = AgentState(row["state"])
                if curr_state != AgentState.AVAILABLE:
                    raise AgentNotAvailableError(
                        f"Cannot reserve agent {agent_id}: current state is {curr_state.value}"
                    )
            
            # Transition from AVAILABLE -> RESERVED
            return await cls._transition_unlocked(
                conn=conn,
                agent_id=agent_id,
                target_state=AgentState.RESERVED,
                call_id=call_id
            )

    @classmethod
    async def _transition_unlocked(
        cls,
        conn: aiosqlite.Connection,
        agent_id: str,
        target_state: AgentState,
        call_id: Optional[str] = None
    ) -> dict:
        # Assumes caller holds lock
        async with conn.execute(
            "SELECT id, name, state, version, current_call_id FROM agents WHERE id = ?", (agent_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise ValueError(f"Agent {agent_id} not found")
            
            curr_state = AgentState(row["state"])
            curr_version = row["version"]

            cls.validate_transition(curr_state, target_state)

            new_version = curr_version + 1
            now = time.time()
            new_call_id = call_id if target_state in {AgentState.RESERVED, AgentState.DIALING, AgentState.CONNECTED} else (
                None if target_state in {AgentState.AVAILABLE, AgentState.OFFLINE, AgentState.PAUSED} else row["current_call_id"]
            )

            cursor_update = await conn.execute(
                """
                UPDATE agents 
                SET state = ?, version = ?, current_call_id = ?, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (target_state.value, new_version, new_call_id, now, agent_id, curr_version)
            )

            if cursor_update.rowcount != 1:
                raise OptimisticLockError(f"Concurrent update failed for agent {agent_id}")

            await conn.commit()
            return {
                "id": agent_id,
                "name": row["name"],
                "state": target_state.value,
                "version": new_version,
                "current_call_id": new_call_id,
                "updated_at": now
            }
