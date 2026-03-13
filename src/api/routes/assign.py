import os
from typing import Optional

from fastapi import APIRouter, Form, Header, HTTPException
from pydantic import BaseModel

from src.api.models import AssignTicketRequest
from src.api.services import auto_assign
from src.api.utils.logging import get_logger, log_event
from src.integrations.liveagent import LiveAgentClient

router = APIRouter()
logger = get_logger(__name__)

class LiveAgentTicketWebHook(BaseModel):
    conv_code: str
    agent_id: Optional[str] = None

class AutoAssignResponse(BaseModel):
    status: str
    conv_code: str
    agent_id: Optional[str] = None
    reason: Optional[str] = None


@router.post("/auto-assign", response_model=AutoAssignResponse)
async def assign(
    conv_code: str = Form(...),
    agent_id: Optional[str] = Form(None),
    x_cmx_auto_assign_secret: Optional[str] = Header(
        None, alias="X-CMX-Auto-Assign-Secret"
    ),
) -> AutoAssignResponse:
    expected_secret = os.getenv("WEBHOOK_SECRET")
    if not expected_secret:
        log_event(logger, "auto_assign_secret_missing", conv_code=conv_code)
        raise HTTPException(status_code=500, detail="Server misconfigured")
    if not x_cmx_auto_assign_secret or x_cmx_auto_assign_secret != expected_secret:
        log_event(logger, "auto_assign_unauthorized", conv_code=conv_code)
        raise HTTPException(status_code=401, detail="Unauthorized")
    ticket = LiveAgentTicketWebHook(
        conv_code=conv_code,
        agent_id=agent_id
    )

    log_event(
        logger,
        "auto_assign_request",
        conv_code=ticket.conv_code,
        incoming_agent_id=ticket.agent_id,
    )

    existing = auto_assign.get_existing_assignment(ticket.conv_code)
    if existing:
        result = {
            "status": "already_assigned",
            "conv_code": ticket.conv_code,
            "agent_id": existing.get("agent_id"),
            "reason": "idempotent_replay",
        }
    else:
        client = LiveAgentClient()
        try:
            remote_ticket = await client.get_ticket(ticket.conv_code)
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
            else:
                plan = auto_assign.plan_next_assignment()
                if plan.get("status") != "candidate":
                    result = {
                        "status": plan.get("status"),
                        "conv_code": ticket.conv_code,
                        "agent_id": plan.get("agent_id"),
                        "reason": plan.get("reason"),
                    }
                else:
                    assign_status = os.getenv("LIVEAGENT_ASSIGN_STATUS", "N").strip()
                    payload = AssignTicketRequest(
                        agent_id=str(plan.get("agent_id")),
                        status=assign_status or "N",
                    )
                    await client.assign_ticket(ticket.conv_code, payload)
                    result = auto_assign.commit_assignment(
                        conv_code=ticket.conv_code,
                        agent_id=str(plan.get("agent_id")),
                        reason=str(plan.get("reason") or "round_robin"),
                        day_key=str(plan.get("day_key")),
                        next_index=plan.get("next_index"),
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

    log_event(
        logger,
        "auto_assign_response",
        conv_code=ticket.conv_code,
        status=str(result.get("status")),
        agent_id=result.get("agent_id"),
        reason=result.get("reason"),
    )
    return AutoAssignResponse(
        status=str(result.get("status")),
        conv_code=ticket.conv_code,
        agent_id=result.get("agent_id"),
        reason=result.get("reason"),
    )
