from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from src.api.routes.admin_agents import _admin_actor, _handle_admin_error
from src.api.services import reassign_settings


router = APIRouter(prefix="/admin")


class ReassignSettingsView(BaseModel):
    enabled: bool
    after_minutes: int
    max_attempts: int
    eligible_statuses: List[str]
    max_age_minutes: int
    max_per_run: int
    active_from: Optional[str] = None
    active_to: Optional[str] = None


class ReassignConfigResponse(BaseModel):
    version: int
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    change_reason: Optional[str] = None
    exists: bool = Field(description="False while no Firestore doc exists (env vars / defaults apply)")
    settings: ReassignSettingsView
    sources: Dict[str, str] = Field(description="Per field: firestore | env | default")


class ReassignUpdateRequest(BaseModel):
    expected_version: int = Field(..., ge=0, description="0 to create the doc for the first time")
    change_reason: str = Field(..., min_length=1)
    enabled: Optional[bool] = None
    after_minutes: Optional[int] = Field(None, ge=1, le=1440)
    max_attempts: Optional[int] = Field(None, ge=0, le=10)
    eligible_statuses: Optional[List[str]] = Field(None, min_length=1)
    max_age_minutes: Optional[int] = Field(None, ge=1, le=10080)
    max_per_run: Optional[int] = Field(None, ge=1, le=500)
    # Office hours, Asia/Manila "HH:MM". Set both, or send both as null to clear.
    active_from: Optional[str] = Field(None, description='e.g. "09:00"; null clears')
    active_to: Optional[str] = Field(None, description='e.g. "18:00"; null clears')


class ReassignMutationResponse(BaseModel):
    status: str
    config: ReassignConfigResponse


@router.get("/reassign", response_model=ReassignConfigResponse)
def get_reassign_settings(actor: str = Depends(_admin_actor)) -> ReassignConfigResponse:
    _ = actor
    return ReassignConfigResponse(**reassign_settings.get_settings_config())


@router.patch("/reassign", response_model=ReassignMutationResponse)
def patch_reassign_settings(
    req: ReassignUpdateRequest,
    actor: str = Depends(_admin_actor),
) -> ReassignMutationResponse:
    changes: Dict[str, Any] = req.model_dump(
        exclude_unset=True, exclude={"expected_version", "change_reason"}
    )
    # null means "leave unchanged" - except for the window bounds, where an
    # explicit null clears the restriction.
    changes = {
        key: value
        for key, value in changes.items()
        if value is not None or key in reassign_settings.WINDOW_FIELDS
    }
    try:
        config = reassign_settings.update_settings(
            expected_version=req.expected_version,
            actor=actor,
            change_reason=req.change_reason,
            changes=changes,
        )
    except Exception as exc:  # pragma: no cover - converted below
        _handle_admin_error(exc)
        raise
    return ReassignMutationResponse(status="ok", config=ReassignConfigResponse(**config))
