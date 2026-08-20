from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Callable, Awaitable

class TelecomProviderError(Exception):
    pass

class TelecomProvider(ABC):
    @abstractmethod
    async def dial(self, call_id: str, phone_number: str) -> Dict[str, Any]:
        """
        Initiates a call.
        Returns dict with provider_call_id and initial status.
        Raises TelecomProviderError on setup failure/timeout.
        """
        pass

    @abstractmethod
    async def hangup(self, provider_call_id: str) -> bool:
        """
        Hangs up an active call.
        """
        pass
