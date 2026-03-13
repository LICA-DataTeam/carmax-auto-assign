from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, ConfigDict


class LiveAgentCustomField(BaseModel):
    code: str
    value: Optional[str] = None


class LiveAgentTicket(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: Optional[str] = None
    conv_code: Optional[str] = None
    agent_id: Optional[str] = Field(default=None, alias="agentid")
    status: Optional[str] = None
    department_id: Optional[str] = Field(default=None, alias="departmentid")
    tags: Optional[List[str]] = None
    custom_fields: Optional[List[LiveAgentCustomField]] = Field(default=None, alias="custom_fields")


class LiveAgentAgent(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    status: Optional[str] = None
    online_status: Optional[str] = None


class AssignTicketRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(alias="agentid")
    status: Optional[str] = None
