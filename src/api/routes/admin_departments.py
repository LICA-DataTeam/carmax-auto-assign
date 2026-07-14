from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from src.api.routes.admin_agents import _admin_actor
from src.api.services import auto_assign, department_routing_admin


router = APIRouter(prefix="/admin")


class DepartmentUpsertRequest(BaseModel):
    label: str = Field(default="")
    mode: Literal["direct", "team_pool", "full_pool"]
    agent_keys: List[str] = Field(default_factory=list)
    active: bool = True
    expected_version: int = Field(..., ge=0)
    change_reason: str = Field(..., min_length=1)
    owner_overrides: Dict[str, str] = Field(
        default_factory=dict,
        description="team_pool only: exact ticket owner_name (case-insensitive) -> agent_key, "
        "for routing an agent's own FB page traffic straight to them even inside a shared department",
    )


class DepartmentDeleteRequest(BaseModel):
    expected_version: int = Field(..., ge=0)
    change_reason: str = Field(..., min_length=1)
    mode: Literal["deactivate", "remove"] = "deactivate"


class DepartmentRoutingConfigResponse(BaseModel):
    version: int
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    change_reason: Optional[str] = None
    departments: Dict[str, Any]
    departments_list: list[Dict[str, Any]]


class DepartmentMutationResponse(BaseModel):
    status: str
    config: DepartmentRoutingConfigResponse
    department: Optional[Dict[str, Any]] = None


class DepartmentPreviewResponse(BaseModel):
    department_id: str
    status: str
    agent_id: Optional[str] = None
    reason: Optional[str] = None
    pool_key: Optional[str] = None
    bypass_quota: bool = False


def _to_config_response(payload: Dict[str, Any]) -> DepartmentRoutingConfigResponse:
    return DepartmentRoutingConfigResponse(
        version=int(payload.get("version", 0) or 0),
        updated_at=payload.get("updated_at"),
        updated_by=payload.get("updated_by"),
        change_reason=payload.get("change_reason"),
        departments=dict(payload.get("departments") or {}),
        departments_list=list(payload.get("departments_list") or []),
    )


def _handle_admin_error(exc: Exception) -> None:
    if isinstance(exc, department_routing_admin.VersionConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, department_routing_admin.NotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, department_routing_admin.ValidationError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.get("/departments", response_model=DepartmentRoutingConfigResponse)
def get_departments(
    actor: str = Depends(_admin_actor),
) -> DepartmentRoutingConfigResponse:
    _ = actor
    payload = department_routing_admin.get_department_routing_config()
    return _to_config_response(payload)


@router.get("/departments/{department_id}/preview", response_model=DepartmentPreviewResponse)
def preview_department(
    department_id: str = Path(..., min_length=1),
    now: Optional[str] = Query(
        None, description="ISO8601 timestamp to simulate (defaults to current time)"
    ),
    exclude_agent_ids: Optional[str] = Query(
        None, description="Comma-separated agent_keys to exclude, to simulate a reassignment"
    ),
    owner_name: Optional[str] = Query(
        None, description="Simulate a ticket's owner_name, to test team_pool owner_overrides"
    ),
    actor: str = Depends(_admin_actor),
) -> DepartmentPreviewResponse:
    """Read-only: shows what plan_next_assignment would decide for this department right now.

    Never calls LiveAgent and never writes to the assignment store or daily counters -
    safe to run against real production config without touching a real ticket.
    """
    _ = actor
    simulated_now: Optional[datetime] = None
    if now:
        try:
            simulated_now = datetime.fromisoformat(now)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="now must be an ISO8601 timestamp") from exc

    exclude_ids = None
    if exclude_agent_ids:
        exclude_ids = {item.strip() for item in exclude_agent_ids.split(",") if item.strip()}

    plan = auto_assign.plan_next_assignment(
        now=simulated_now,
        department_id=department_id,
        exclude_agent_ids=exclude_ids,
        owner_name=owner_name,
    )
    return DepartmentPreviewResponse(
        department_id=department_id,
        status=str(plan.get("status")),
        agent_id=plan.get("agent_id"),
        reason=plan.get("reason"),
        pool_key=plan.get("pool_key"),
        bypass_quota=bool(plan.get("bypass_quota", False)),
    )


def _upsert_department(
    req: DepartmentUpsertRequest,
    department_id: str,
    actor: str,
) -> DepartmentMutationResponse:
    try:
        result = department_routing_admin.upsert_department_route(
            department_id=department_id,
            label=req.label,
            mode=req.mode,
            agent_keys=req.agent_keys,
            active=req.active,
            expected_version=req.expected_version,
            actor=actor,
            change_reason=req.change_reason,
            owner_overrides=req.owner_overrides,
        )
    except Exception as exc:  # pragma: no cover - converted below
        _handle_admin_error(exc)
        raise
    return DepartmentMutationResponse(
        status="ok",
        config=_to_config_response(result.config),
        department=result.changed_department,
    )


@router.post("/departments/{department_id}", response_model=DepartmentMutationResponse)
def create_or_replace_department(
    req: DepartmentUpsertRequest,
    department_id: str = Path(..., min_length=1),
    actor: str = Depends(_admin_actor),
) -> DepartmentMutationResponse:
    return _upsert_department(req, department_id, actor)


@router.patch("/departments/{department_id}", response_model=DepartmentMutationResponse)
def update_department(
    req: DepartmentUpsertRequest,
    department_id: str = Path(..., min_length=1),
    actor: str = Depends(_admin_actor),
) -> DepartmentMutationResponse:
    return _upsert_department(req, department_id, actor)


@router.delete("/departments/{department_id}", response_model=DepartmentMutationResponse)
def remove_department(
    req: DepartmentDeleteRequest,
    department_id: str = Path(..., min_length=1),
    actor: str = Depends(_admin_actor),
) -> DepartmentMutationResponse:
    try:
        result = department_routing_admin.delete_department_route(
            target_department_id=department_id,
            expected_version=req.expected_version,
            actor=actor,
            change_reason=req.change_reason,
            mode=req.mode,
        )
    except Exception as exc:  # pragma: no cover - converted below
        _handle_admin_error(exc)
        raise
    return DepartmentMutationResponse(
        status="ok",
        config=_to_config_response(result.config),
        department=result.changed_department,
    )
