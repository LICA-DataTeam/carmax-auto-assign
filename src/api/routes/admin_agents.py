from __future__ import annotations

import json
import os
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Security
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from src.api.services import agent_config_admin


ADMIN_TOKENS_ENV = "ADMIN_BEARER_TOKENS"
ADMIN_TOKEN_MAP_ENV = "ADMIN_BEARER_TOKEN_MAP"
ADMIN_ALLOWED_PRINCIPALS_ENV = "ADMIN_ALLOWED_PRINCIPALS"


router = APIRouter(prefix="/admin")
_bearer = HTTPBearer(auto_error=False)


class AgentCreateRequest(BaseModel):
    team: str = Field(..., min_length=1)
    agent_key: str = Field(..., min_length=1)
    agent_name: str = Field(..., min_length=1)
    active: bool = True
    expected_version: int = Field(..., ge=0)
    change_reason: str = Field(..., min_length=1)
    target: int = Field(..., ge=0)
    min: int = Field(..., ge=0)
    max: int = Field(..., ge=0)


class AgentUpdateRequest(BaseModel):
    expected_version: int = Field(..., ge=0)
    change_reason: str = Field(..., min_length=1)
    team: Optional[str] = None
    agent_name: Optional[str] = None
    active: Optional[bool] = None
    target: int = Field(..., ge=0)
    min: int = Field(..., ge=0)
    max: int = Field(..., ge=0)


class AgentDeleteRequest(BaseModel):
    expected_version: int = Field(..., ge=0)
    change_reason: str = Field(..., min_length=1)
    mode: Literal["deactivate", "remove"] = "deactivate"


class AdminConfigResponse(BaseModel):
    version: int
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    change_reason: Optional[str] = None
    teams: list[Dict[str, Any]]
    agents: list[Dict[str, Any]]


class AdminMutationResponse(BaseModel):
    status: str
    config: AdminConfigResponse
    agent: Optional[Dict[str, Any]] = None


def _split_csv(raw: str) -> set[str]:
    return {item.strip() for item in raw.split(",") if item.strip()}


def _normalize_principal(raw: str) -> str:
    value = raw.strip()
    if ":" in value:
        _, tail = value.split(":", 1)
        return tail.strip().lower()
    return value.lower()


def _token_principal_map() -> Dict[str, str]:
    raw = os.getenv(ADMIN_TOKEN_MAP_ENV, "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Invalid ADMIN_BEARER_TOKEN_MAP") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="Invalid ADMIN_BEARER_TOKEN_MAP")

    mapped: Dict[str, str] = {}
    for token, principal in payload.items():
        token_value = str(token or "").strip()
        principal_value = str(principal or "").strip()
        if token_value and principal_value:
            mapped[token_value] = principal_value.lower()
    return mapped


def _require_admin(
    credentials: Optional[HTTPAuthorizationCredentials],
    iap_user_email: Optional[str],
) -> str:
    configured_tokens = _split_csv(os.getenv(ADMIN_TOKENS_ENV, ""))
    mapped_tokens = _token_principal_map()
    configured_principals = {value.lower() for value in _split_csv(os.getenv(ADMIN_ALLOWED_PRINCIPALS_ENV, ""))}

    if not configured_tokens and not configured_principals and not mapped_tokens:
        raise HTTPException(status_code=500, detail="Admin auth not configured")

    if credentials and credentials.credentials:
        token = credentials.credentials.strip()
        if token:
            principal = mapped_tokens.get(token)
            if principal:
                return principal
            if token in configured_tokens:
                return "token_admin"

    if iap_user_email and configured_principals:
        principal = _normalize_principal(iap_user_email)
        if principal in configured_principals:
            return principal

    raise HTTPException(status_code=401, detail="Unauthorized")


def _admin_actor(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    x_goog_authenticated_user_email: Optional[str] = Header(None),
) -> str:
    return _require_admin(credentials, x_goog_authenticated_user_email)


def _to_config_response(payload: Dict[str, Any]) -> AdminConfigResponse:
    return AdminConfigResponse(
        version=int(payload.get("version", 0) or 0),
        updated_at=payload.get("updated_at"),
        updated_by=payload.get("updated_by"),
        change_reason=payload.get("change_reason"),
        teams=list(payload.get("teams") or []),
        agents=list(payload.get("agents") or []),
    )


def _handle_admin_error(exc: Exception) -> None:
    if isinstance(exc, agent_config_admin.VersionConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, agent_config_admin.NotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, agent_config_admin.ValidationError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.get("/agents", response_model=AdminConfigResponse)
def get_agents(
    actor: str = Depends(_admin_actor),
) -> AdminConfigResponse:
    _ = actor
    payload = agent_config_admin.get_agent_config()
    return _to_config_response(payload)


@router.post("/agents", response_model=AdminMutationResponse)
def create_agent(
    req: AgentCreateRequest,
    actor: str = Depends(_admin_actor),
) -> AdminMutationResponse:
    try:
        result = agent_config_admin.create_agent(
            team=req.team,
            agent_key=req.agent_key,
            agent_name=req.agent_name,
            active=req.active,
            expected_version=req.expected_version,
            actor=actor,
            change_reason=req.change_reason,
            target=req.target,
            min_value=req.min,
            max_value=req.max,
        )
    except Exception as exc:  # pragma: no cover - converted below
        _handle_admin_error(exc)
        raise
    return AdminMutationResponse(
        status="ok",
        config=_to_config_response(result.config),
        agent=result.changed_agent,
    )


@router.patch("/agents/{agent_key}", response_model=AdminMutationResponse)
def patch_agent(
    req: AgentUpdateRequest,
    agent_key: str = Path(..., min_length=1),
    actor: str = Depends(_admin_actor),
) -> AdminMutationResponse:
    try:
        result = agent_config_admin.update_agent(
            target_agent_key=agent_key,
            expected_version=req.expected_version,
            actor=actor,
            change_reason=req.change_reason,
            team=req.team,
            agent_name=req.agent_name,
            active=req.active,
            target=req.target,
            min_value=req.min,
            max_value=req.max,
        )
    except Exception as exc:  # pragma: no cover - converted below
        _handle_admin_error(exc)
        raise
    return AdminMutationResponse(
        status="ok",
        config=_to_config_response(result.config),
        agent=result.changed_agent,
    )


@router.delete("/agents/{agent_key}", response_model=AdminMutationResponse)
def remove_agent(
    req: AgentDeleteRequest,
    agent_key: str = Path(..., min_length=1),
    actor: str = Depends(_admin_actor),
) -> AdminMutationResponse:
    try:
        result = agent_config_admin.delete_agent(
            target_agent_key=agent_key,
            expected_version=req.expected_version,
            actor=actor,
            change_reason=req.change_reason,
            mode=req.mode,
        )
    except Exception as exc:  # pragma: no cover - converted below
        _handle_admin_error(exc)
        raise
    return AdminMutationResponse(
        status="ok",
        config=_to_config_response(result.config),
        agent=result.changed_agent,
    )


@router.get("/ui", response_class=HTMLResponse)
def admin_ui(actor: str = Depends(_admin_actor)) -> str:
    _ = actor
    return """<!DOCTYPE html>
<html>
<head>
  <meta charset='utf-8' />
  <meta name='viewport' content='width=device-width, initial-scale=1' />
  <title>CMX Admin Agents</title>
  <style>
    body { font-family: ui-sans-serif, system-ui, sans-serif; margin: 24px; }
    input, textarea, button, select { margin: 4px 0; padding: 6px; width: 100%; max-width: 520px; }
    .row { margin-bottom: 16px; }
    pre { background: #f4f4f4; padding: 12px; border-radius: 8px; overflow: auto; }
  </style>
</head>
<body>
  <h2>CMX Admin Agent Config</h2>
  <div class='row'>
    <label>Bearer token</label>
    <input id='token' type='password' placeholder='ADMIN_BEARER_TOKENS value' />
    <button onclick='loadConfig()'>Load Agents</button>
  </div>

  <div class='row'>
    <h3>Create Agent</h3>
    <input id='createTeam' placeholder='Team' />
    <input id='createKey' placeholder='Agent key' />
    <input id='createName' placeholder='Agent name' />
    <input id='createTarget' type='number' placeholder='Target' />
    <input id='createMin' type='number' placeholder='Min' />
    <input id='createMax' type='number' placeholder='Max' />
    <input id='createVersion' type='number' placeholder='Expected version' />
    <input id='createReason' placeholder='Change reason' />
    <button onclick='createAgent()'>Create</button>
  </div>

  <div class='row'>
    <h3>Update Agent</h3>
    <input id='updateKey' placeholder='Agent key' />
    <input id='updateName' placeholder='New name (optional)' />
    <input id='updateTarget' type='number' placeholder='Target' />
    <input id='updateMin' type='number' placeholder='Min' />
    <input id='updateMax' type='number' placeholder='Max' />
    <input id='updateVersion' type='number' placeholder='Expected version' />
    <input id='updateReason' placeholder='Change reason' />
    <button onclick='updateAgent()'>Update</button>
  </div>

  <div class='row'>
    <h3>Delete Agent</h3>
    <input id='deleteKey' placeholder='Agent key' />
    <input id='deleteVersion' type='number' placeholder='Expected version' />
    <input id='deleteReason' placeholder='Change reason' />
    <select id='deleteMode'>
      <option value='deactivate'>deactivate</option>
      <option value='remove'>remove</option>
    </select>
    <button onclick='deleteAgent()'>Delete</button>
  </div>

  <div class='row'>
    <h3>Department Routing</h3>
    <button onclick='loadDepartments()'>Load Departments</button>
  </div>

  <div class='row'>
    <h3>Preview Department Routing (read-only, no ticket touched)</h3>
    <input id='previewDeptId' placeholder='Department id' />
    <input id='previewNow' placeholder='Simulate time (ISO8601, optional)' />
    <input id='previewExclude' placeholder='Exclude agent_keys (comma-separated, optional)' />
    <input id='previewOwnerName' placeholder='Simulate ticket owner_name (optional, tests owner_overrides)' />
    <button onclick='previewDepartment()'>Preview</button>
  </div>

  <div class='row'>
    <h3>Upsert Department Route</h3>
    <input id='deptId' placeholder='Department id (LiveAgent departmentid)' />
    <input id='deptLabel' placeholder='Label' />
    <select id='deptMode'>
      <option value='direct'>direct</option>
      <option value='team_pool'>team_pool</option>
      <option value='full_pool'>full_pool</option>
    </select>
    <input id='deptAgentKeys' placeholder='agent_keys (comma-separated)' />
    <label><input id='deptActive' type='checkbox' checked style='width:auto;display:inline-block' /> active</label>
    <textarea id='deptOwnerOverrides' rows='3' placeholder='team_pool only: owner_overrides as JSON, e.g. {"Carmax Authorized Agent - Kent Viado": "rljq27v2"}'></textarea>
    <input id='deptVersion' type='number' placeholder='Expected version' />
    <input id='deptReason' placeholder='Change reason' />
    <button onclick='upsertDepartment()'>Save</button>
  </div>

  <div class='row'>
    <h3>Delete Department Route</h3>
    <input id='deptDeleteId' placeholder='Department id' />
    <input id='deptDeleteVersion' type='number' placeholder='Expected version' />
    <input id='deptDeleteReason' placeholder='Change reason' />
    <select id='deptDeleteMode'>
      <option value='deactivate'>deactivate</option>
      <option value='remove'>remove</option>
    </select>
    <button onclick='deleteDepartment()'>Delete</button>
  </div>

  <pre id='output'></pre>
  <script>
    function authHeader() {
      const token = document.getElementById('token').value.trim();
      return token ? { 'Authorization': 'Bearer ' + token } : {};
    }
    async function api(path, method='GET', body=null) {
      const res = await fetch(path, {
        method,
        headers: { 'Content-Type': 'application/json', ...authHeader() },
        body: body ? JSON.stringify(body) : null
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) throw payload;
      return payload;
    }
    function write(payload) {
      document.getElementById('output').textContent = JSON.stringify(payload, null, 2);
    }
    async function loadConfig() {
      try { write(await api('/admin/agents')); } catch (e) { write(e); }
    }
    async function createAgent() {
      try {
        write(await api('/admin/agents', 'POST', {
          team: document.getElementById('createTeam').value,
          agent_key: document.getElementById('createKey').value,
          agent_name: document.getElementById('createName').value,
          active: true,
          target: Number(document.getElementById('createTarget').value),
          min: Number(document.getElementById('createMin').value),
          max: Number(document.getElementById('createMax').value),
          expected_version: Number(document.getElementById('createVersion').value),
          change_reason: document.getElementById('createReason').value
        }));
      } catch (e) { write(e); }
    }
    async function updateAgent() {
      const key = document.getElementById('updateKey').value;
      try {
        write(await api('/admin/agents/' + encodeURIComponent(key), 'PATCH', {
          agent_name: document.getElementById('updateName').value || null,
          target: Number(document.getElementById('updateTarget').value),
          min: Number(document.getElementById('updateMin').value),
          max: Number(document.getElementById('updateMax').value),
          expected_version: Number(document.getElementById('updateVersion').value),
          change_reason: document.getElementById('updateReason').value
        }));
      } catch (e) { write(e); }
    }
    async function deleteAgent() {
      const key = document.getElementById('deleteKey').value;
      try {
        write(await api('/admin/agents/' + encodeURIComponent(key), 'DELETE', {
          expected_version: Number(document.getElementById('deleteVersion').value),
          change_reason: document.getElementById('deleteReason').value,
          mode: document.getElementById('deleteMode').value
        }));
      } catch (e) { write(e); }
    }
    async function loadDepartments() {
      try { write(await api('/admin/departments')); } catch (e) { write(e); }
    }
    async function previewDepartment() {
      const id = document.getElementById('previewDeptId').value;
      const params = new URLSearchParams();
      const now = document.getElementById('previewNow').value.trim();
      const exclude = document.getElementById('previewExclude').value.trim();
      const ownerName = document.getElementById('previewOwnerName').value.trim();
      if (now) params.set('now', now);
      if (exclude) params.set('exclude_agent_ids', exclude);
      if (ownerName) params.set('owner_name', ownerName);
      const qs = params.toString();
      try {
        write(await api('/admin/departments/' + encodeURIComponent(id) + '/preview' + (qs ? '?' + qs : '')));
      } catch (e) { write(e); }
    }
    async function upsertDepartment() {
      const id = document.getElementById('deptId').value;
      const agentKeys = document.getElementById('deptAgentKeys').value
        .split(',').map(s => s.trim()).filter(Boolean);
      const overridesRaw = document.getElementById('deptOwnerOverrides').value.trim();
      let ownerOverrides = {};
      if (overridesRaw) {
        try { ownerOverrides = JSON.parse(overridesRaw); }
        catch (e) { write({error: 'owner_overrides must be valid JSON: ' + e}); return; }
      }
      try {
        write(await api('/admin/departments/' + encodeURIComponent(id), 'POST', {
          label: document.getElementById('deptLabel').value,
          mode: document.getElementById('deptMode').value,
          agent_keys: agentKeys,
          active: document.getElementById('deptActive').checked,
          owner_overrides: ownerOverrides,
          expected_version: Number(document.getElementById('deptVersion').value),
          change_reason: document.getElementById('deptReason').value
        }));
      } catch (e) { write(e); }
    }
    async function deleteDepartment() {
      const id = document.getElementById('deptDeleteId').value;
      try {
        write(await api('/admin/departments/' + encodeURIComponent(id), 'DELETE', {
          expected_version: Number(document.getElementById('deptDeleteVersion').value),
          change_reason: document.getElementById('deptDeleteReason').value,
          mode: document.getElementById('deptDeleteMode').value
        }));
      } catch (e) { write(e); }
    }
  </script>
</body>
</html>
"""
