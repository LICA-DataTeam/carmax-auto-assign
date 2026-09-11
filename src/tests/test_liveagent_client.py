from __future__ import annotations

import httpx
import pytest

from src.integrations.liveagent.client import LiveAgentClient
from src.integrations.liveagent.config import LiveAgentSettings


def _settings(**overrides) -> LiveAgentSettings:
    base = dict(
        base_url="https://example.ladesk.com/api/v3",
        api_key="test-key",
        api_header="apikey",
        timeout_seconds=5.0,
        verify_ssl=True,
        max_agent_pages=50,
    )
    base.update(overrides)
    return LiveAgentSettings(**base)


def _agent(agent_id: str) -> dict:
    return {"id": agent_id, "name": agent_id, "status": "A", "online_status": "N"}


def _client_with_transport(handler, **settings_overrides) -> LiveAgentClient:
    client = LiveAgentClient(settings=_settings(**settings_overrides))
    client._client = httpx.AsyncClient(
        base_url=client.settings.base_url,
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.anyio
async def test_list_agents_walks_pagination_until_short_page() -> None:
    # Regression test: LiveAgent's real GET /agents silently paginates
    # (confirmed live: a bare call returned only 10 of 38 agents), so a
    # naive single request would silently drop most of the roster.
    pages = {
        1: [_agent(f"a{i}") for i in range(100)],
        2: [_agent(f"b{i}") for i in range(100)],
        3: [_agent("c0")],
    }
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("_page"))
        requested_pages.append(page)
        assert request.url.params.get("_perPage") == "100"
        return httpx.Response(200, json=pages.get(page, []))

    client = _client_with_transport(handler)
    try:
        agents = await client.list_agents()
    finally:
        await client.close()

    assert requested_pages == [1, 2, 3]
    assert [a.id for a in agents] == (
        [f"a{i}" for i in range(100)] + [f"b{i}" for i in range(100)] + ["c0"]
    )


@pytest.mark.anyio
async def test_list_agents_single_short_page_stops_after_one_request() -> None:
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_pages.append(int(request.url.params.get("_page")))
        return httpx.Response(200, json=[_agent("only")])

    client = _client_with_transport(handler)
    try:
        agents = await client.list_agents()
    finally:
        await client.close()

    assert requested_pages == [1]
    assert [a.id for a in agents] == ["only"]


@pytest.mark.anyio
async def test_list_agents_supports_dict_wrapped_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("_page"))
        items = [_agent("wrapped")] if page == 1 else []
        return httpx.Response(200, json={"data": items})

    client = _client_with_transport(handler)
    try:
        agents = await client.list_agents()
    finally:
        await client.close()

    assert [a.id for a in agents] == ["wrapped"]


@pytest.mark.anyio
async def test_list_agents_respects_max_agent_pages_safety_cap() -> None:
    # If the API ever ignored _page and always returned a full page, this
    # cap keeps list_agents() from looping forever.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_agent("x")] * 100)

    client = _client_with_transport(handler, max_agent_pages=2)
    try:
        agents = await client.list_agents()
    finally:
        await client.close()

    assert len(agents) == 200
