import os
from typing import Optional

from fastapi import APIRouter, Form, Header, HTTPException
from pydantic import BaseModel

from src.api.services.auto_assign import assign_round_robin
from src.api.utils.logging import get_logger, log_event

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
    result = assign_round_robin(
        conv_code=ticket.conv_code,
        incoming_agent_id=ticket.agent_id,
    )
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
