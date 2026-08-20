import os
import time
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, HTTPException, Query, BackgroundTask
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

from app.db.database import get_db_connection, init_db
from app.state_machines.agent_state import AgentState, AgentStateMachine
from app.state_machines.call_state import CallState, CallStateMachine
from app.providers.provider_a import ProviderA
from app.providers.provider_b import ProviderB
from app.pacing.pacing_engine import PredictivePacingEngine
from app.safety.safety_controller import SafetyController
from app.allocator.call_allocator import CallAllocator
from app.progressive.progressive_dialer import ProgressiveDialer
from app.campaign.campaign_manager import CampaignManager
from app.reconciliation.recovery import WorkerCrashRecoveryEngine

app = FastAPI(
    title="SmartDialer Predictive Call Center Engine",
    description="Production-grade SmartDialer prototype in Predictive & Progressive Mode with asyncio locking & SQLite",
    version="1.0.0"
)

# Global Instances initialized on startup
provider_a = ProviderA()
provider_b = ProviderB()
pacing_engine = PredictivePacingEngine()
progressive_dialer = ProgressiveDialer(provider=provider_a)
safety_controller = SafetyController(target_abandon_rate=0.03, progressive_dialer=progressive_dialer)
call_allocator = CallAllocator(provider=provider_a)
campaign_manager = CampaignManager(pacing_engine, safety_controller, call_allocator, progressive_dialer)
recovery_engine = WorkerCrashRecoveryEngine()

@app.on_event("startup")
async def on_startup():
    await init_db()

class AgentCreateRequest(BaseModel):
    agent_id: str
    name: str

class AgentStateTransitionRequest(BaseModel):
    target_state: AgentState

class BorrowerCreateRequest(BaseModel):
    borrower_id: str
    name: str
    phone_number: str

@app.get("/")
async def root():
    return {
        "system": "SmartDialer Engine",
        "status": "ONLINE",
        "documentation": "/docs",
        "scenario_report": "/report"
    }

@app.post("/agents", summary="Create or seed agent")
async def create_agent(req: AgentCreateRequest):
    conn = await get_db_connection()
    try:
        now = time.time()
        await conn.execute(
            """
            INSERT OR REPLACE INTO agents (id, name, state, version, updated_at)
            VALUES (?, ?, 'OFFLINE', 1, ?)
            """,
            (req.agent_id, req.name, now)
        )
        await conn.commit()
        return {"agent_id": req.agent_id, "name": req.name, "state": "OFFLINE"}
    finally:
        await conn.close()

@app.post("/agents/{agent_id}/state", summary="Transition agent state")
async def transition_agent(agent_id: str, req: AgentStateTransitionRequest):
    conn = await get_db_connection()
    try:
        res = await AgentStateMachine.transition(conn, agent_id, req.target_state)
        return res
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await conn.close()

@app.get("/agents", summary="List all agents")
async def list_agents():
    conn = await get_db_connection()
    try:
        async with conn.execute("SELECT id, name, state, version, current_call_id, updated_at FROM agents") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
    finally:
        await conn.close()

@app.post("/borrowers", summary="Add borrower to queue")
async def create_borrower(req: BorrowerCreateRequest):
    conn = await get_db_connection()
    try:
        now = time.time()
        await conn.execute(
            """
            INSERT OR REPLACE INTO borrowers (id, name, phone_number, attempts, status, updated_at)
            VALUES (?, ?, ?, 0, 'PENDING', ?)
            """,
            (req.borrower_id, req.name, req.phone_number, now)
        )
        await conn.commit()
        return {"borrower_id": req.borrower_id, "status": "PENDING"}
    finally:
        await conn.close()

@app.post("/campaigns/{campaign_id}/tick", summary="Trigger Predictive Pacing Pipeline Tick")
async def trigger_pacing_tick(campaign_id: str):
    conn = await get_db_connection()
    try:
        res = await campaign_manager.execute_pacing_tick(conn, campaign_id)
        return res
    finally:
        await conn.close()

@app.post("/reconciliation/run", summary="Trigger Worker Crash Reconciliation")
async def run_reconciliation():
    conn = await get_db_connection()
    try:
        res = await recovery_engine.reconcile_orphans(conn)
        return res
    finally:
        await conn.close()

@app.get("/report", response_class=HTMLResponse, summary="View Scenario Benchmark Report")
async def view_report():
    report_path = os.path.join("reports", "scenario_report.html")
    if not os.path.exists(report_path):
        return "<html><body><h2>No scenario report generated yet. Run: <code>python -m simulations.scenario_runner</code></h2></body></html>"
    with open(report_path, "r", encoding="utf-8") as f:
        return f.read()
