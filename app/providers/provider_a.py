import asyncio
import random
import time
import uuid
from typing import Dict, Any
from .base import TelecomProvider, TelecomProviderError

class ProviderA(TelecomProvider):
    def __init__(self, failure_rate: float = 0.01, setup_latency: float = 0.2):
        self.failure_rate = failure_rate
        self.setup_latency = setup_latency

    async def dial(self, call_id: str, phone_number: str) -> Dict[str, Any]:
        await asyncio.sleep(self.setup_latency)
        if random.random() < self.failure_rate:
            raise TelecomProviderError(f"ProviderA: Setup failed for call {call_id}")
        
        provider_call_id = f"pA-{uuid.uuid4().hex[:8]}"
        return {
            "provider_id": "ProviderA",
            "provider_call_id": provider_call_id,
            "status": "INITIATED",
            "timestamp": time.time()
        }

    async def hangup(self, provider_call_id: str) -> bool:
        await asyncio.sleep(0.05)
        return True
