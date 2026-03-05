from typing import Optional

from fastapi import APIRouter, Form
from pydantic import BaseModel

router = APIRouter()

class LiveAgentTicketWebHook(BaseModel):
    conv_code: str
    agent_id: Optional[str] = None

class AutoAssignResponse(BaseModel):
    status: str
    conv_code: str
    agent_id: Optional[str] = None


@router.post("/assign", response_model=AutoAssignResponse)
async def assign(
    conv_code: str = Form(...),
    agent_id: Optional[str] = Form(None)
) -> AutoAssignResponse:
    ticket = LiveAgentTicketWebHook(
        conv_code=conv_code,
        agent_id=agent_id
    )

    # placeholder
    # example ticket already has agent id
    if ticket.agent_id:
        status = "skipped"
        return AutoAssignResponse(
            status=status,
            conv_code=ticket.conv_code,
            agent_id=ticket.agent_id
        )

    status = "assigned"
    new_agent_id = "test"
    return AutoAssignResponse(
        status=status,
        conv_code=ticket.conv_code,
        agent_id=new_agent_id
    )