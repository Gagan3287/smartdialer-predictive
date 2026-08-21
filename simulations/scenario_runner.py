import asyncio
import os
import time
import json
import logging
from typing import Dict, Any, List
import aiosqlite

from app.db.database import get_db_connection, init_db
from app.state_machines.agent_state import AgentState, AgentLockRegistry
from app.state_machines.call_state import CallLockRegistry
from app.providers.provider_a import ProviderA
from app.providers.provider_b import ProviderB
from app.pacing.pacing_engine import PredictivePacingEngine
from app.safety.safety_controller import SafetyController, SafetyAction
from app.allocator.call_allocator import CallAllocator
from app.progressive.progressive_dialer import ProgressiveDialer
from app.campaign.campaign_manager import CampaignManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scenario_runner")

SCENARIO_SPECS = {
    "Scenario A": {
        "description": "Low Answer Rate (20%), Standard Talk Time (120s), ProviderA",
        "answer_rate": 0.20,
        "talk_time": 120.0,
        "setup_time": 10.0,
        "provider": "ProviderA",
        "agents": 50,
        "borrowers": 200,
        "ticks": 10
    },
    "Scenario B": {
        "description": "Medium Answer Rate (50%), Short Talk Time (90s), ProviderA",
        "answer_rate": 0.50,
        "talk_time": 90.0,
        "setup_time": 10.0,
        "provider": "ProviderA",
        "agents": 50,
        "borrowers": 200,
        "ticks": 10
    },
    "Scenario C": {
        "description": "High Answer Rate (70%), Long Talk Time (180s), ProviderA",
        "answer_rate": 0.70,
        "talk_time": 180.0,
        "setup_time": 10.0,
        "provider": "ProviderA",
        "agents": 50,
        "borrowers": 200,
        "ticks": 10
    },
    "Scenario D": {
        "description": "Dynamic Answer Rate (30% -> 60%), Variable Talk Time, Flaky ProviderB Latency/Failures",
        "answer_rate": 0.30,
        "talk_time": 120.0,
        "setup_time": 15.0,
        "provider": "ProviderB",
        "agents": 50,
        "borrowers": 200,
        "ticks": 10
    }
}

async def run_scenario(name: str, spec: dict) -> dict:
    db_path = f"sim_{name.replace(' ', '_').lower()}.db"
    os.environ["SMARTDIALER_DB_PATH"] = db_path
    AgentLockRegistry.reset()
    CallLockRegistry.reset()
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except PermissionError:
            pass

    import app.db.database as db_mod
    db_mod.DB_PATH = db_path
    await init_db()

    conn = await get_db_connection()
    now = time.time()

    # Seed Agents & Borrowers
    for i in range(1, spec["agents"] + 1):
        await conn.execute(
            "INSERT INTO agents (id, name, state, version, updated_at) VALUES (?, ?, ?, 1, ?)",
            (f"agent-{i}", f"Agent {i}", AgentState.AVAILABLE.value, now)
        )
    for i in range(1, spec["borrowers"] + 1):
        await conn.execute(
            "INSERT INTO borrowers (id, name, phone_number, attempts, status, updated_at) VALUES (?, ?, ?, 0, 'PENDING', ?)",
            (f"borr-{i}", f"Borrower {i}", f"+1555{i:04d}", now)
        )
    await conn.commit()

    if spec["provider"] == "ProviderA":
        provider = ProviderA(failure_rate=0.01, setup_latency=0.02)
    else:
        provider = ProviderB(failure_rate=0.15, min_latency=0.1, max_latency=0.3)

    pacing = PredictivePacingEngine(default_setup_time=spec["setup_time"], default_talk_time=spec["talk_time"])
    prog_dialer = ProgressiveDialer(provider=provider)
    safety = SafetyController(target_abandon_rate=0.03, progressive_dialer=prog_dialer)
    allocator = CallAllocator(provider=provider, simulated_answer_rate=spec["answer_rate"])
    campaign = CampaignManager(pacing, safety, allocator, prog_dialer)

    tick_history = []
    safety_actions_count = {"APPROVE": 0, "REDUCE": 0, "REJECT": 0, "FALLBACK_PROGRESSIVE": 0}
    total_calls_initiated = 0

    for tick in range(1, spec["ticks"] + 1):
        res = await campaign.execute_pacing_tick(
            conn, f"camp-{name}", answer_rate_override=spec["answer_rate"]
        )
        action = res["safety"]["action"]
        safety_actions_count[action] = safety_actions_count.get(action, 0) + 1
        total_calls_initiated += res["allocated_calls_count"]

        # Calculate agent utilization
        metrics = res["metrics"]
        total_agents = spec["agents"]
        active_agents = total_agents - metrics["available_agents"]
        utilization_pct = (active_agents / total_agents) * 100.0

        tick_history.append({
            "tick": tick,
            "utilization_pct": round(utilization_pct, 2),
            "available_agents": metrics["available_agents"],
            "busy_agents": metrics["busy_agents"],
            "recommended_n": res["pacing"]["recommended_calls"],
            "safety_action": action,
            "approved_calls": res["safety"]["approved_calls"],
            "allocated_count": res["allocated_calls_count"]
        })

    # Summary Stats
    avg_utilization = sum(h["utilization_pct"] for h in tick_history) / len(tick_history)

    await conn.close()
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except PermissionError:
            pass

    return {
        "name": name,
        "description": spec["description"],
        "avg_utilization_pct": round(avg_utilization, 2),
        "total_calls_initiated": total_calls_initiated,
        "safety_actions_count": safety_actions_count,
        "tick_history": tick_history
    }

def generate_html_report(results: List[dict]) -> str:
    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>SmartDialer Predictive Mode Scenario Report</title>
        <style>
            body {{ font-family: 'Segoe UI', Arial, sans-serif; margin: 30px; background-color: #0f172a; color: #f8fafc; }}
            h1 {{ color: #38bdf8; border-bottom: 2px solid #334155; padding-bottom: 10px; }}
            .card {{ background: #1e293b; border-radius: 8px; padding: 20px; margin-bottom: 25px; border: 1px solid #334155; }}
            .card h2 {{ color: #f1f5f9; margin-top: 0; }}
            .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin: 15px 0; }}
            .stat-box {{ background: #0f172a; padding: 15px; border-radius: 6px; text-align: center; border: 1px solid #334155; }}
            .stat-value {{ font-size: 24px; font-weight: bold; color: #38bdf8; }}
            .stat-label {{ font-size: 12px; color: #94a3b8; margin-top: 5px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 14px; }}
            th, td {{ border: 1px solid #334155; padding: 10px; text-align: center; }}
            th {{ background-color: #334155; color: #f8fafc; }}
            tr:nth-child(even) {{ background-color: #1e293b; }}
            tr:nth-child(odd) {{ background-color: #0f172a; }}
            .tag {{ padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; }}
            .tag-APPROVE {{ background: #059669; color: white; }}
            .tag-REDUCE {{ background: #d97706; color: white; }}
            .tag-REJECT {{ background: #dc2626; color: white; }}
            .tag-FALLBACK_PROGRESSIVE {{ background: #7c3aed; color: white; }}
        </style>
    </head>
    <body>
        <h1>SmartDialer Predictive Mode Benchmark Report</h1>
        <p style="color: #94a3b8;">Automated performance evaluation across Scenarios A/B/C/D</p>
    """

    for res in results:
        html += f"""
        <div class="card">
            <h2>{res['name']}</h2>
            <p style="color: #cbd5e1;"><em>{res['description']}</em></p>
            <div class="stats-grid">
                <div class="stat-box">
                    <div class="stat-value">{res['avg_utilization_pct']}%</div>
                    <div class="stat-label">Avg Agent Utilization</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{res['total_calls_initiated']}</div>
                    <div class="stat-label">Total Calls Initiated</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{res['safety_actions_count'].get('APPROVE', 0)}</div>
                    <div class="stat-label">Pacing Approved</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{res['safety_actions_count'].get('REDUCE', 0)}</div>
                    <div class="stat-label">Safety Capped (Reduced)</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{res['safety_actions_count'].get('FALLBACK_PROGRESSIVE', 0)}</div>
                    <div class="stat-label">Progressive Fallbacks</div>
                </div>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Tick</th>
                        <th>Utilization</th>
                        <th>Available Agents</th>
                        <th>Busy Agents</th>
                        <th>Pacing Rec (N)</th>
                        <th>Safety Action</th>
                        <th>Allocated Calls</th>
                    </tr>
                </thead>
                <tbody>
        """
        for h in res["tick_history"]:
            html += f"""
                    <tr>
                        <td>{h['tick']}</td>
                        <td>{h['utilization_pct']}%</td>
                        <td>{h['available_agents']}</td>
                        <td>{h['busy_agents']}</td>
                        <td>{h['recommended_n']}</td>
                        <td><span class="tag tag-{h['safety_action']}">{h['safety_action']}</span></td>
                        <td>{h['allocated_count']}</td>
                    </tr>
            """
        html += """
                </tbody>
            </table>
        </div>
        """

    html += """
    </body>
    </html>
    """
    return html

async def main():
    logger.info("Running Scenario Suite A/B/C/D...")
    results = []
    for name, spec in SCENARIO_SPECS.items():
        logger.info(f"Running {name}: {spec['description']}")
        res = await run_scenario(name, spec)
        results.append(res)
        logger.info(
            f"{name} Complete -> Avg Util: {res['avg_utilization_pct']}%, "
            f"Calls Initiated: {res['total_calls_initiated']}, "
            f"Safety Actions: {res['safety_actions_count']}"
        )

    os.makedirs("reports", exist_ok=True)
    report_path = os.path.join("reports", "scenario_report.html")
    html_content = generate_html_report(results)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    logger.info(f"HTML Scenario Benchmark Report generated at: {os.path.abspath(report_path)}")

if __name__ == "__main__":
    asyncio.run(main())