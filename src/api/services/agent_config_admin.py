from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.api.services import auto_assign


AGENTS_FIRESTORE_COLLECTION_ENV = "FIRESTORE_COLLECTION_AGENT_CONFIG"
AGENTS_FIRESTORE_DOC_ENV = "FIRESTORE_DOC_AGENT_CONFIG"
FIRESTORE_PROJECT_ENV = "FIRESTORE_PROJECT_ID"
FIRESTORE_DATABASE_ENV = "FIRESTORE_DATABASE_ID"
FIRESTORE_CREDENTIALS_ENV = "CREDENTIALS"
AUDIT_COLLECTION_ENV = "FIRESTORE_COLLECTION_AGENT_CONFIG_AUDIT"


class AdminConfigError(Exception):
    pass


class VersionConflictError(AdminConfigError):
    pass


class ValidationError(AdminConfigError):
    pass


class NotFoundError(AdminConfigError):
    pass


@dataclass(frozen=True)
class AgentMutationResult:
    config: Dict[str, object]
    changed_agent: Optional[Dict[str, object]]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_firestore_client():
    try:
        from google.cloud import firestore
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError("google-cloud-firestore is required for admin agent config") from exc

    creds_raw = os.getenv(FIRESTORE_CREDENTIALS_ENV, "").strip()
    credentials = None
    project_id = os.getenv(FIRESTORE_PROJECT_ENV, "").strip() or None

    if creds_raw:
        info = None
        if creds_raw.startswith("{"):
            info = json.loads(creds_raw)
        else:
            path = Path(creds_raw)
            if path.exists():
                info = json.loads(path.read_text(encoding="utf-8"))
        if info:
            credentials = service_account.Credentials.from_service_account_info(info)
            project_id = project_id or info.get("project_id")

    database_id = os.getenv(FIRESTORE_DATABASE_ENV, "(default)").strip() or "(default)"
    client = firestore.Client(
        project=project_id or None,
        credentials=credentials,
        database=database_id,
    )
    return client, firestore


def _config_ref(client):
    collection_name = os.getenv(AGENTS_FIRESTORE_COLLECTION_ENV, "app_config").strip() or "app_config"
    doc_id = os.getenv(AGENTS_FIRESTORE_DOC_ENV, "carmax_agents").strip() or "carmax_agents"
    return client.collection(collection_name).document(doc_id)


def _audit_collection(client):
    collection_name = os.getenv(AUDIT_COLLECTION_ENV, "agent_config_audit").strip() or "agent_config_audit"
    return client.collection(collection_name)


def _normalize_agent(raw: Dict[str, object], team_name: str) -> Dict[str, object]:
    return {
        "team": team_name,
        "agent_key": raw.get("agent_key"),
        "agent_name": raw.get("agent_name"),
        "active": raw.get("active"),
        "target": raw.get("target"),
        "min": raw.get("min"),
        "max": raw.get("max"),
    }


def _list_agents(payload: Dict[str, object]) -> List[Dict[str, object]]:
    results: List[Dict[str, object]] = []
    teams = payload.get("teams") or []
    for team in teams:
        if not isinstance(team, dict):
            continue
        team_name = str(team.get("team") or "").strip()
        for item in team.get("agents") or []:
            if not isinstance(item, dict):
                continue
            results.append(_normalize_agent(item, team_name))
    return results


def _find_agent(teams: List[Dict[str, object]], agent_key: str) -> Optional[Tuple[int, int]]:
    for team_idx, team in enumerate(teams):
        for agent_idx, item in enumerate(team.get("agents") or []):
            if str(item.get("agent_key") or "").strip() == agent_key:
                return team_idx, agent_idx
    return None


def _find_or_create_team(teams: List[Dict[str, object]], team_name: str) -> Dict[str, object]:
    for team in teams:
        if str(team.get("team") or "").strip() == team_name:
            agents = team.get("agents")
            if not isinstance(agents, list):
                team["agents"] = []
            return team
    new_team = {"team": team_name, "agents": []}
    teams.append(new_team)
    return new_team


def _validate_payload(payload: Dict[str, object]) -> None:
    try:
        auto_assign._parse_agents_payload(payload, source="admin_write")
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _apply_metadata(
    payload: Dict[str, object],
    *,
    new_version: int,
    actor: str,
    change_reason: str,
    updated_at: str,
) -> Dict[str, object]:
    payload["version"] = new_version
    payload["updated_at"] = updated_at
    payload["updated_by"] = actor
    payload["change_reason"] = change_reason
    return payload


def _write_audit_entry(
    *,
    txn,
    audit_collection,
    actor: str,
    change_reason: str,
    operation: str,
    before: Dict[str, object],
    after: Dict[str, object],
    before_agent: Optional[Dict[str, object]],
    after_agent: Optional[Dict[str, object]],
    expected_version: int,
) -> None:
    audit_ref = audit_collection.document()
    audit_record = {
        "timestamp": _utc_now_iso(),
        "actor": actor,
        "change_reason": change_reason,
        "operation": operation,
        "expected_version": expected_version,
        "before_version": before.get("version", 0),
        "after_version": after.get("version", 0),
        "before_agent": before_agent,
        "after_agent": after_agent,
        "before": before,
        "after": after,
    }
    txn.set(audit_ref, audit_record)


def get_agent_config() -> Dict[str, object]:
    client, _ = _get_firestore_client()
    snap = _config_ref(client).get()
    payload = snap.to_dict() if snap.exists else {"teams": [], "version": 0}
    if not isinstance(payload, dict):
        raise ValidationError("Agent config document must be an object")
    _validate_payload(payload)
    payload.setdefault("version", 0)
    payload.setdefault("updated_at", None)
    payload.setdefault("updated_by", None)
    payload.setdefault("change_reason", None)
    payload["agents"] = _list_agents(payload)
    return payload


def create_agent(
    *,
    team: str,
    agent_key: str,
    agent_name: str,
    active: bool,
    expected_version: int,
    actor: str,
    change_reason: str,
    target: Optional[int] = None,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
) -> AgentMutationResult:
    team_name = team.strip()
    normalized_key = agent_key.strip()
    normalized_name = agent_name.strip()
    if not team_name:
        raise ValidationError("team is required")
    if not normalized_key:
        raise ValidationError("agent_key is required")
    if not normalized_name:
        raise ValidationError("agent_name is required")
    if not change_reason.strip():
        raise ValidationError("change_reason is required")

    client, firestore = _get_firestore_client()
    config_ref = _config_ref(client)
    audit_collection = _audit_collection(client)
    txn = client.transaction()

    @firestore.transactional
    def _create(transaction):
        snap = config_ref.get(transaction=transaction)
        current = snap.to_dict() if snap.exists else {"teams": [], "version": 0}
        if not isinstance(current, dict):
            raise ValidationError("Agent config document must be an object")

        current_version = int(current.get("version", 0) or 0)
        if current_version != expected_version:
            raise VersionConflictError(
                f"version conflict: expected {expected_version}, got {current_version}"
            )

        updated = deepcopy(current)
        teams = updated.setdefault("teams", [])
        if not isinstance(teams, list):
            raise ValidationError("Agent config 'teams' must be a list")

        if _find_agent(teams, normalized_key):
            raise ValidationError(f"Duplicate agent_key found: {normalized_key}")

        team_entry = _find_or_create_team(teams, team_name)
        agent = {
            "agent_key": normalized_key,
            "agent_name": normalized_name,
            "active": bool(active),
        }
        if target is not None:
            agent["target"] = int(target)
        if min_value is not None:
            agent["min"] = int(min_value)
        if max_value is not None:
            agent["max"] = int(max_value)
        team_entry.setdefault("agents", []).append(agent)

        _validate_payload(updated)
        new_version = current_version + 1
        updated_at = _utc_now_iso()
        _apply_metadata(
            updated,
            new_version=new_version,
            actor=actor,
            change_reason=change_reason,
            updated_at=updated_at,
        )
        transaction.set(config_ref, updated)
        _write_audit_entry(
            txn=transaction,
            audit_collection=audit_collection,
            actor=actor,
            change_reason=change_reason,
            operation="create_agent",
            before=current,
            after=updated,
            before_agent=None,
            after_agent=_normalize_agent(agent, team_name),
            expected_version=expected_version,
        )
        return updated, _normalize_agent(agent, team_name)

    updated_payload, changed_agent = _create(txn)
    updated_payload["agents"] = _list_agents(updated_payload)
    return AgentMutationResult(config=updated_payload, changed_agent=changed_agent)


def update_agent(
    *,
    target_agent_key: str,
    expected_version: int,
    actor: str,
    change_reason: str,
    team: Optional[str] = None,
    agent_name: Optional[str] = None,
    active: Optional[bool] = None,
    target: Optional[int] = None,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
) -> AgentMutationResult:
    normalized_key = target_agent_key.strip()
    if not normalized_key:
        raise ValidationError("agent_key is required")
    if not change_reason.strip():
        raise ValidationError("change_reason is required")

    client, firestore = _get_firestore_client()
    config_ref = _config_ref(client)
    audit_collection = _audit_collection(client)
    txn = client.transaction()

    @firestore.transactional
    def _update(transaction):
        snap = config_ref.get(transaction=transaction)
        if not snap.exists:
            raise NotFoundError("Agent config not found")
        current = snap.to_dict() or {}
        if not isinstance(current, dict):
            raise ValidationError("Agent config document must be an object")

        current_version = int(current.get("version", 0) or 0)
        if current_version != expected_version:
            raise VersionConflictError(
                f"version conflict: expected {expected_version}, got {current_version}"
            )

        updated = deepcopy(current)
        teams = updated.setdefault("teams", [])
        if not isinstance(teams, list):
            raise ValidationError("Agent config 'teams' must be a list")

        location = _find_agent(teams, normalized_key)
        if location is None:
            raise NotFoundError(f"Agent not found: {normalized_key}")
        from_team_idx, agent_idx = location
        from_team = teams[from_team_idx]
        source_team_name = str(from_team.get("team") or "").strip()
        agent = from_team.setdefault("agents", [])[agent_idx]
        before_agent = _normalize_agent(agent, source_team_name)

        destination_team_name = team.strip() if team is not None else source_team_name
        if destination_team_name:
            destination_team = _find_or_create_team(teams, destination_team_name)
        else:
            destination_team = from_team
            destination_team_name = source_team_name

        if destination_team is not from_team:
            moved_agent = dict(agent)
            from_team_agents = from_team.get("agents") or []
            from_team["agents"] = [item for item in from_team_agents if item is not agent]
            destination_team.setdefault("agents", []).append(moved_agent)
            agent = moved_agent

        if agent_name is not None:
            normalized_name = agent_name.strip()
            if not normalized_name:
                raise ValidationError("agent_name cannot be empty")
            agent["agent_name"] = normalized_name
        if active is not None:
            agent["active"] = bool(active)
        if target is not None:
            agent["target"] = int(target)
        if min_value is not None:
            agent["min"] = int(min_value)
        if max_value is not None:
            agent["max"] = int(max_value)

        _validate_payload(updated)
        new_version = current_version + 1
        updated_at = _utc_now_iso()
        _apply_metadata(
            updated,
            new_version=new_version,
            actor=actor,
            change_reason=change_reason,
            updated_at=updated_at,
        )
        transaction.set(config_ref, updated)
        after_agent = _normalize_agent(agent, destination_team_name)
        _write_audit_entry(
            txn=transaction,
            audit_collection=audit_collection,
            actor=actor,
            change_reason=change_reason,
            operation="update_agent",
            before=current,
            after=updated,
            before_agent=before_agent,
            after_agent=after_agent,
            expected_version=expected_version,
        )
        return updated, after_agent

    updated_payload, changed_agent = _update(txn)
    updated_payload["agents"] = _list_agents(updated_payload)
    return AgentMutationResult(config=updated_payload, changed_agent=changed_agent)


def delete_agent(
    *,
    target_agent_key: str,
    expected_version: int,
    actor: str,
    change_reason: str,
    mode: str = "deactivate",
) -> AgentMutationResult:
    normalized_key = target_agent_key.strip()
    normalized_mode = mode.strip().lower()
    if normalized_mode not in {"deactivate", "remove"}:
        raise ValidationError("mode must be 'deactivate' or 'remove'")
    if not normalized_key:
        raise ValidationError("agent_key is required")
    if not change_reason.strip():
        raise ValidationError("change_reason is required")

    client, firestore = _get_firestore_client()
    config_ref = _config_ref(client)
    audit_collection = _audit_collection(client)
    txn = client.transaction()

    @firestore.transactional
    def _delete(transaction):
        snap = config_ref.get(transaction=transaction)
        if not snap.exists:
            raise NotFoundError("Agent config not found")
        current = snap.to_dict() or {}
        if not isinstance(current, dict):
            raise ValidationError("Agent config document must be an object")

        current_version = int(current.get("version", 0) or 0)
        if current_version != expected_version:
            raise VersionConflictError(
                f"version conflict: expected {expected_version}, got {current_version}"
            )

        updated = deepcopy(current)
        teams = updated.setdefault("teams", [])
        if not isinstance(teams, list):
            raise ValidationError("Agent config 'teams' must be a list")

        location = _find_agent(teams, normalized_key)
        if location is None:
            raise NotFoundError(f"Agent not found: {normalized_key}")
        team_idx, agent_idx = location
        team_entry = teams[team_idx]
        team_name = str(team_entry.get("team") or "").strip()
        team_agents = team_entry.setdefault("agents", [])
        target_agent = team_agents[agent_idx]
        before_agent = _normalize_agent(target_agent, team_name)

        if normalized_mode == "remove":
            del team_agents[agent_idx]
        else:
            target_agent["active"] = False

        _validate_payload(updated)
        new_version = current_version + 1
        updated_at = _utc_now_iso()
        _apply_metadata(
            updated,
            new_version=new_version,
            actor=actor,
            change_reason=change_reason,
            updated_at=updated_at,
        )
        transaction.set(config_ref, updated)
        after_agent = None if normalized_mode == "remove" else _normalize_agent(target_agent, team_name)
        _write_audit_entry(
            txn=transaction,
            audit_collection=audit_collection,
            actor=actor,
            change_reason=change_reason,
            operation=f"{normalized_mode}_agent",
            before=current,
            after=updated,
            before_agent=before_agent,
            after_agent=after_agent,
            expected_version=expected_version,
        )
        return updated, after_agent

    updated_payload, changed_agent = _delete(txn)
    updated_payload["agents"] = _list_agents(updated_payload)
    return AgentMutationResult(config=updated_payload, changed_agent=changed_agent)
