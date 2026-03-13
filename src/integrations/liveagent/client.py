from __future__ import annotations

from typing import List, Optional

import httpx

from src.api.models.liveagent import AssignTicketRequest, LiveAgentAgent, LiveAgentTicket
from src.integrations.liveagent.config import LiveAgentSettings, load_liveagent_settings


class LiveAgentClient:
    def __init__(self, settings: Optional[LiveAgentSettings] = None) -> None:
        self.settings = settings or load_liveagent_settings()
        if not self.settings.api_key:
            raise ValueError("LIVEAGENT_API_KEY is required")
        self._client = httpx.AsyncClient(
            base_url=self.settings.base_url,
            timeout=self.settings.timeout_seconds,
            verify=self.settings.verify_ssl,
        )

    def _headers(self) -> dict[str, str]:
        return {self.settings.api_header: self.settings.api_key}

    async def get_ticket(self, ticket_id: str) -> LiveAgentTicket:
        resp = await self._client.get(f"/tickets/{ticket_id}", headers=self._headers())
        resp.raise_for_status()
        return LiveAgentTicket.model_validate(resp.json())

    async def assign_ticket(self, ticket_id: str, payload: AssignTicketRequest) -> LiveAgentTicket:
        resp = await self._client.put(
            f"/ticket/{ticket_id}",
            headers=self._headers(),
            json=payload.model_dump(by_alias=True, exclude_none=True),
        )
        resp.raise_for_status()
        return LiveAgentTicket.model_validate(resp.json())

    async def list_agents(self) -> List[LiveAgentAgent]:
        resp = await self._client.get("/agents", headers=self._headers())
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return [LiveAgentAgent.model_validate(item) for item in data]
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
            return [LiveAgentAgent.model_validate(item) for item in data["data"]]
        return []

    async def close(self) -> None:
        await self._client.aclose()
