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

    async def get_ticket_messages(self, ticket_id: str) -> object:
        resp = await self._client.get(f"/tickets/{ticket_id}/messages", headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    async def assign_ticket(self, ticket_id: str, payload: AssignTicketRequest) -> LiveAgentTicket:
        resp = await self._client.put(
            f"/tickets/{ticket_id}",
            headers=self._headers(),
            json=payload.model_dump(by_alias=True, exclude_none=True),
        )
        resp.raise_for_status()
        return LiveAgentTicket.model_validate(resp.json())

    async def list_agents(self) -> List[LiveAgentAgent]:
        """LiveAgent silently paginates GET /agents (confirmed live: a bare
        call returns only the first 10 of 38 agents) so this walks pages
        until a short/empty page signals the end, instead of trusting a
        single call to return the full roster."""
        agents: List[LiveAgentAgent] = []
        page = 1
        per_page = 100
        while page <= self.settings.max_agent_pages:
            resp = await self._client.get(
                "/agents",
                headers=self._headers(),
                params={"_page": page, "_perPage": per_page},
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                items = data["data"]
            else:
                items = []
            agents.extend(LiveAgentAgent.model_validate(item) for item in items)
            if len(items) < per_page:
                break
            page += 1
        return agents

    async def close(self) -> None:
        await self._client.aclose()
