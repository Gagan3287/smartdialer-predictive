import asyncio
import random
import time
import uuid
from typing import Dict, Any
from .base import TelecomProvider, TelecomProviderError

class ProviderB(TelecomProvider):
    def __init__(self, failure_rate: float = 0.15, min_latency: float = 1.0, max_latency: float = 3.0):
        self.failure_rate = failure_rate
        self.min_latency = min_latency
        self.max_latency = max_latency

    async def dial(self, call_id: str, phone_number: str) -> Dict[str, Any]:
        latency = random.uniform(self.min_latency, self.max_latency)
        await asyncio.sleep(latency)
        
        if random.random() < self.failure_rate:
            raise TelecomProviderError(f"ProviderB: Network timeout/failure for call {call_id} after {latency:.2f}s")
        
        provider_call_id = f"pB-{uuid.uuid4().hex[:8]}"
        return {
            "provider_id": "ProviderB",
            "provider_call_id": provider_call_id,
            "status": "INITIATED",
            "timestamp": time.time()
        }

    async def hangup(self, provider_call_id: str) -> bool:
        await asyncio.sleep(0.1)
        return True
