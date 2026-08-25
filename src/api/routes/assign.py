import os
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Form, Header, HTTPException, Query
from pydantic import BaseModel

from src.api.models import AssignTicketRequest
from src.api.services.bq_logger import log_bq_event
from src.api.services import auto_assign, run_auto_reassign
from src.api.utils.logging import get_logger, log_event
from src.integrations.liveagent import LiveAgentClient, extract_private_message_page_name

router = APIRouter()
logger = get_logger(__name__)


async def _resolve_owner_override_fallback_name(
    client: LiveAgentClient, conv_code: str
) -> Optional[str]:
    """When a team_pool department's owner_overrides map misses on the
    ticket's own owner_name (e.g. a private FB message where owner_name
    carries the customer's name, not the page name), fetch the ticket's
    messages and look for LiveAgent's "Private message to: <a>PAGE</a>"
    system message as a fallback signal of which page it landed on."""
    try:
        messages_payload = await client.get_ticket_messages(conv_code)
    except Exception as exc:
        log_event(
            logger,
            "auto_assign_owner_override_fallback_fetch_failed",
            conv_code=conv_code,
            error=str(exc),
        )
        return None
    return extract_private_message_page_name(messages_payload)


async def _fetch_offline_agent_ids(client: LiveAgentClient, conv_code: str) -> set[str]:
    """An employed agent (status "A") who is currently offline in LiveAgent
    (online_status "F") is excluded from candidate selection so assignment
    falls through to the next online agent. Fails open: if the live agent
    list can't be fetched, returns an empty set so assignment still proceeds
    against the existing eligible pool instead of blocking on this side call."""
    try:
        agents = await client.list_agents()
    except Exception as exc:
        log_event(
            logger,
            "auto_assign_online_status_fetch_failed",
            conv_code=conv_code,
            error=str(exc),
        )
        return set()
    return {
        str(agent.id)
        for agent in agents
        if agent.id and agent.status == "A" and agent.online_status == "F"
    }


class LiveAgentTicketWebHook(BaseModel):
    conv_code: str
    agent_id: Optional[str] = None

class AutoAssignResponse(BaseModel):
    status: str
    conv_code: str
    agent_id: Optional[str] = None
    reason: Optional[str] = None


def _require_secret(secret: Optional[str], *, conv_code: Optional[str] = None) -> None:
    expected_secret = os.getenv("WEBHOOK_SECRET")
    if not expected_secret:
        log_event(logger, "auto_assign_secret_missing", conv_code=conv_code)
        raise HTTPException(status_code=500, detail="Server misconfigured")
    if not secret or secret != expected_secret:
        log_event(logger, "auto_assign_unauthorized", conv_code=conv_code)
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.post("/auto-assign", response_model=AutoAssignResponse)
async def assign(
    conv_code: str = Form(...),
    agent_id: Optional[str] = Form(None),
    x_cmx_auto_assign_secret: Optional[str] = Header(
        None, alias="X-CMX-Auto-Assign-Secret"
    ),
) -> AutoAssignResponse:
    _require_secret(x_cmx_auto_assign_secret, conv_code=conv_code)
    ticket = LiveAgentTicketWebHook(
        conv_code=conv_code,
        agent_id=agent_id
    )
    request_id = uuid.uuid4().hex
    start_time = time.perf_counter()

    log_event(
        logger,
        "auto_assign_request",
        conv_code=ticket.conv_code,
        incoming_agent_id=ticket.agent_id,
    )
    log_bq_event(
        "auto_assign_request",
        request_id=request_id,
        conv_code=ticket.conv_code,
        incoming_agent_id=ticket.agent_id,
    )

    department_id: Optional[str] = None
    existing = auto_assign.get_existing_assignment(ticket.conv_code)
    if existing:
        result = {
            "status": "already_assigned",
            "conv_code": ticket.conv_code,
            "agent_id": existing.get("agent_id"),
            "reason": "idempotent_replay",
        }
        log_bq_event(
            "auto_assign_decision",
            request_id=request_id,
            conv_code=ticket.conv_code,
            status="already_assigned",
            agent_id=existing.get("agent_id"),
            reason="idempotent_replay",
        )
    else:
        client = LiveAgentClient()
        try:
            remote_ticket = await client.get_ticket(ticket.conv_code)
            department_id = remote_ticket.department_id
            remote_agent = remote_ticket.agent_id or ticket.agent_id
            if remote_agent:
                auto_assign.record_existing_assignment(
                    conv_code=ticket.conv_code,
                    agent_id=remote_agent,
                    reason="ticket_already_assigned",
                )
                result = {
                    "status": "already_assigned",
                    "conv_code": ticket.conv_code,
                    "agent_id": remote_agent,
                    "reason": "ticket_already_assigned",
                }
                log_bq_event(
                    "auto_assign_decision",
                    request_id=request_id,
                    conv_code=ticket.conv_code,
                    status="already_assigned",
                    agent_id=remote_agent,
                    reason="ticket_already_assigned",
                )
            else:
                offline_agent_ids = await _fetch_offline_agent_ids(client, ticket.conv_code)
                plan = auto_assign.plan_next_assignment(
                    department_id=department_id,
                    owner_name=remote_ticket.owner_name,
                    exclude_agent_ids=offline_agent_ids,
                )
                if (
                    plan.get("status") == "candidate"
                    and plan.get("reason") == "team_pool"
                    and plan.get("owner_overrides_configured")
                ):
                    fallback_owner_name = await _resolve_owner_override_fallback_name(
                        client, ticket.conv_code
                    )
                    if fallback_owner_name:
                        fallback_plan = auto_assign.plan_next_assignment(
                            department_id=department_id,
                            owner_name=fallback_owner_name,
                            owner_name_source="ticket_message_fallback",
                            exclude_agent_ids=offline_agent_ids,
                        )
                        if fallback_plan.get("reason") == "team_pool_owner_override_message_fallback":
                            plan = fallback_plan
                if plan.get("status") != "candidate":
                    result = {
                        "status": plan.get("status"),
                        "conv_code": ticket.conv_code,
                        "agent_id": plan.get("agent_id"),
                        "reason": plan.get("reason"),
                    }
                    log_bq_event(
                        "auto_assign_decision",
                        request_id=request_id,
                        conv_code=ticket.conv_code,
                        status=plan.get("status"),
                        agent_id=plan.get("agent_id"),
                        reason=plan.get("reason"),
                        department_id=department_id,
                    )
                else:
                    assign_status = os.getenv("LIVEAGENT_ASSIGN_STATUS", "N").strip()
                    payload = AssignTicketRequest(
                        agent_id=str(plan.get("agent_id")),
                        status=assign_status or "N",
                    )
                    log_bq_event(
                        "auto_assign_decision",
                        request_id=request_id,
                        conv_code=ticket.conv_code,
                        status="candidate",
                        agent_id=plan.get("agent_id"),
                        reason=plan.get("reason"),
                        department_id=department_id,
                    )
                    await client.assign_ticket(ticket.conv_code, payload)
                    result = auto_assign.commit_assignment(
                        conv_code=ticket.conv_code,
                        agent_id=str(plan.get("agent_id")),
                        reason=str(plan.get("reason") or "round_robin"),
                        day_key=str(plan.get("day_key")),
                        next_index=plan.get("next_index"),
                        pool_key=str(plan.get("pool_key") or auto_assign.FULL_POOL_KEY),
                        bypass_quota=bool(plan.get("bypass_quota", False)),
                    )
        except Exception as exc:
            log_event(
                logger,
                "auto_assign_liveagent_failed",
                conv_code=ticket.conv_code,
                error=str(exc),
            )
            result = {
                "status": "failed",
                "conv_code": ticket.conv_code,
                "agent_id": None,
                "reason": "liveagent_error",
            }
        finally:
            await client.close()

    latency_ms = int((time.perf_counter() - start_time) * 1000)
    outcome_event = "auto_assign_success"
    if result.get("status") not in {"assigned", "already_assigned"}:
        outcome_event = "auto_assign_failed"
    log_bq_event(
        outcome_event,
        request_id=request_id,
        conv_code=ticket.conv_code,
        status=result.get("status"),
        agent_id=result.get("agent_id"),
        reason=result.get("reason"),
        latency_ms=latency_ms,
        department_id=department_id,
    )
    log_event(
        logger,
        "auto_assign_response",
        conv_code=ticket.conv_code,
        status=str(result.get("status")),
        agent_id=result.get("agent_id"),
        reason=result.get("reason"),
        department_id=department_id,
    )
    return AutoAssignResponse(
        status=str(result.get("status")),
        conv_code=ticket.conv_code,
        agent_id=result.get("agent_id"),
        reason=result.get("reason"),
    )


@router.post("/auto-reassign")
async def auto_reassign(
    limit: Optional[int] = Query(None),
    x_cmx_auto_assign_secret: Optional[str] = Header(
        None, alias="X-CMX-Auto-Assign-Secret"
    ),
) -> dict:
    _require_secret(x_cmx_auto_assign_secret)
    request_id = uuid.uuid4().hex
    result = await run_auto_reassign(limit=limit, request_id=request_id)
    log_event(
        logger,
        "auto_reassign_complete",
        checked=result.get("checked"),
        reassigned=result.get("reassigned"),
    )
    log_bq_event(
        "auto_reassign_complete",
        request_id=request_id,
        checked=result.get("checked"),
        reassigned=result.get("reassigned"),
    )
    return result
