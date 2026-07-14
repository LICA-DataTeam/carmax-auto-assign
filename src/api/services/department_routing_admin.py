from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.api.services import auto_assign


AGENTS_FIRESTORE_COLLECTION_ENV = "FIRESTORE_COLLECTION_AGENT_CONFIG"
DEPARTMENT_ROUTING_DOC_ENV = "FIRESTORE_DOC_DEPARTMENT_ROUTING"
FIRESTORE_PROJECT_ENV = "FIRESTORE_PROJECT_ID"
FIRESTORE_DATABASE_ENV = "FIRESTORE_DATABASE_ID"
FIRESTORE_CREDENTIALS_ENV = "CREDENTIALS"
AUDIT_COLLECTION_ENV = "FIRESTORE_COLLECTION_DEPARTMENT_ROUTING_AUDIT"


class AdminConfigError(Exception):
    pass


class VersionConflictError(AdminConfigError):
    pass


class ValidationError(AdminConfigError):
    pass


class NotFoundError(AdminConfigError):
    pass


@dataclass(frozen=True)
class DepartmentMutationResult:
    config: Dict[str, object]
    changed_department: Optional[Dict[str, object]]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_firestore_client():
    try:
        from google.cloud import firestore
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError("google-cloud-firestore is required for admin department routing config") from exc

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
    doc_id = os.getenv(DEPARTMENT_ROUTING_DOC_ENV, "department_routing").strip() or "department_routing"
    return client.collection(collection_name).document(doc_id)


def _audit_collection(client):
    collection_name = os.getenv(AUDIT_COLLECTION_ENV, "department_routing_audit").strip() or "department_routing_audit"
    return client.collection(collection_name)


def _normalize_department(department_id: str, raw: Dict[str, object]) -> Dict[str, object]:
    return {
        "department_id": department_id,
        "label": raw.get("label"),
        "mode": raw.get("mode"),
        "agent_keys": list(raw.get("agent_keys") or []),
        "active": raw.get("active"),
        "owner_overrides": dict(raw.get("owner_overrides") or {}),
    }


def _list_departments(payload: Dict[str, object]) -> List[Dict[str, object]]:
    departments = payload.get("departments") or {}
    results: List[Dict[str, object]] = []
    for department_id, raw in departments.items():
        if not isinstance(raw, dict):
            continue
        results.append(_normalize_department(str(department_id), raw))
    return results


def _validate_payload(payload: Dict[str, object]) -> None:
    try:
        auto_assign._parse_department_routing_payload(payload, source="admin_write")
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
    before_department: Optional[Dict[str, object]],
    after_department: Optional[Dict[str, object]],
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
        "before_department": before_department,
        "after_department": after_department,
        "before": before,
        "after": after,
    }
    txn.set(audit_ref, audit_record)


def get_department_routing_config() -> Dict[str, object]:
    client, _ = _get_firestore_client()
    snap = _config_ref(client).get()
    payload = snap.to_dict() if snap.exists else {"departments": {}, "version": 0}
    if not isinstance(payload, dict):
        raise ValidationError("Department routing config document must be an object")
    _validate_payload(payload)
    payload.setdefault("version", 0)
    payload.setdefault("updated_at", None)
    payload.setdefault("updated_by", None)
    payload.setdefault("change_reason", None)
    payload["departments_list"] = _list_departments(payload)
    return payload


def upsert_department_route(
    *,
    department_id: str,
    label: str,
    mode: str,
    agent_keys: List[str],
    active: bool,
    expected_version: int,
    actor: str,
    change_reason: str,
    owner_overrides: Optional[Dict[str, str]] = None,
) -> DepartmentMutationResult:
    normalized_id = department_id.strip()
    normalized_mode = mode.strip().lower()
    normalized_keys = [key.strip() for key in agent_keys if key.strip()]
    normalized_overrides = dict(owner_overrides or {})
    if not normalized_id:
        raise ValidationError("department_id is required")
    if normalized_id == auto_assign.DEFAULT_DEPARTMENT_ID:
        raise ValidationError("department_id 'default' is reserved and cannot be routed")
    if normalized_mode not in auto_assign.VALID_ROUTING_MODES:
        raise ValidationError(f"mode must be one of {sorted(auto_assign.VALID_ROUTING_MODES)}")
    if auto_assign.ADMIN_LOGIN_AGENT_KEY in normalized_keys:
        raise ValidationError(
            f"agent_key '{auto_assign.ADMIN_LOGIN_AGENT_KEY}' is the shared admin login and cannot be assignable"
        )
    if not change_reason.strip():
        raise ValidationError("change_reason is required")

    client, firestore = _get_firestore_client()
    config_ref = _config_ref(client)
    audit_collection = _audit_collection(client)
    txn = client.transaction()

    @firestore.transactional
    def _upsert(transaction):
        snap = config_ref.get(transaction=transaction)
        current = snap.to_dict() if snap.exists else {"departments": {}, "version": 0}
        if not isinstance(current, dict):
            raise ValidationError("Department routing config document must be an object")

        current_version = int(current.get("version", 0) or 0)
        if current_version != expected_version:
            raise VersionConflictError(
                f"version conflict: expected {expected_version}, got {current_version}"
            )

        updated = deepcopy(current)
        departments = updated.setdefault("departments", {})
        if not isinstance(departments, dict):
            raise ValidationError("Department routing 'departments' must be an object")

        before_department = None
        existing_raw = departments.get(normalized_id)
        if isinstance(existing_raw, dict):
            before_department = _normalize_department(normalized_id, existing_raw)

        department = {
            "label": label.strip(),
            "mode": normalized_mode,
            "agent_keys": normalized_keys,
            "active": bool(active),
            "owner_overrides": normalized_overrides,
        }
        departments[normalized_id] = department

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
        after_department = _normalize_department(normalized_id, department)
        _write_audit_entry(
            txn=transaction,
            audit_collection=audit_collection,
            actor=actor,
            change_reason=change_reason,
            operation="upsert_department_route",
            before=current,
            after=updated,
            before_department=before_department,
            after_department=after_department,
            expected_version=expected_version,
        )
        return updated, after_department

    updated_payload, changed_department = _upsert(txn)
    updated_payload["departments_list"] = _list_departments(updated_payload)
    return DepartmentMutationResult(config=updated_payload, changed_department=changed_department)


def delete_department_route(
    *,
    target_department_id: str,
    expected_version: int,
    actor: str,
    change_reason: str,
    mode: str = "deactivate",
) -> DepartmentMutationResult:
    normalized_id = target_department_id.strip()
    normalized_mode = mode.strip().lower()
    if normalized_mode not in {"deactivate", "remove"}:
        raise ValidationError("mode must be 'deactivate' or 'remove'")
    if not normalized_id:
        raise ValidationError("department_id is required")
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
            raise NotFoundError("Department routing config not found")
        current = snap.to_dict() or {}
        if not isinstance(current, dict):
            raise ValidationError("Department routing config document must be an object")

        current_version = int(current.get("version", 0) or 0)
        if current_version != expected_version:
            raise VersionConflictError(
                f"version conflict: expected {expected_version}, got {current_version}"
            )

        updated = deepcopy(current)
        departments = updated.setdefault("departments", {})
        if not isinstance(departments, dict):
            raise ValidationError("Department routing 'departments' must be an object")

        existing_raw = departments.get(normalized_id)
        if not isinstance(existing_raw, dict):
            raise NotFoundError(f"Department not found: {normalized_id}")
        before_department = _normalize_department(normalized_id, existing_raw)

        if normalized_mode == "remove":
            del departments[normalized_id]
            after_department = None
        else:
            existing_raw["active"] = False
            after_department = _normalize_department(normalized_id, existing_raw)

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
            operation=f"{normalized_mode}_department_route",
            before=current,
            after=updated,
            before_department=before_department,
            after_department=after_department,
            expected_version=expected_version,
        )
        return updated, after_department

    updated_payload, changed_department = _delete(txn)
    updated_payload["departments_list"] = _list_departments(updated_payload)
    return DepartmentMutationResult(config=updated_payload, changed_department=changed_department)
