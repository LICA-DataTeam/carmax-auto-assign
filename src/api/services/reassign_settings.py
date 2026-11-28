"""Auto-reassign settings: Firestore doc (editable via /admin/reassign) over env vars.

Precedence per field: Firestore `app_config/reassign_settings` value -> Cloud Run
env var -> built-in default. If the Firestore doc does not exist (or the service
reads agents from a file), only env vars/defaults apply, which keeps today's
behavior: `enabled` defaults to false.
"""

from __future__ import annotations

import os
import threading
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.api.services import agent_config_admin
from src.api.services.agent_config_admin import NotFoundError, ValidationError, VersionConflictError
from src.api.utils.logging import get_logger, log_event

logger = get_logger(__name__)

AGENTS_SOURCE_ENV = "AGENTS_SOURCE"
SETTINGS_COLLECTION_ENV = "FIRESTORE_COLLECTION_AGENT_CONFIG"  # default: app_config
SETTINGS_DOC_ENV = "FIRESTORE_DOC_REASSIGN_SETTINGS"  # default: reassign_settings
SETTINGS_AUDIT_COLLECTION_ENV = "FIRESTORE_COLLECTION_REASSIGN_SETTINGS_AUDIT"  # default: reassign_settings_audit

# How long one instance reuses settings it read before reading Firestore again.
CACHE_SECONDS = 30

DEFAULT_ENABLED = False
DEFAULT_AFTER_MINUTES = 10
DEFAULT_MAX_ATTEMPTS = 1
DEFAULT_ELIGIBLE_STATUSES = ("C", "A")
DEFAULT_MAX_AGE_MINUTES = 120
DEFAULT_MAX_PER_RUN = 50

FIELDS = (
    "enabled",
    "after_minutes",
    "max_attempts",
    "eligible_statuses",
    "max_age_minutes",
    "max_per_run",
)

# NotFoundError is re-exported so routes can map every admin error the same way.
__all__ = [
    "ReassignSettings",
    "load_settings",
    "get_settings_config",
    "update_settings",
    "reset_cache",
    "NotFoundError",
    "ValidationError",
    "VersionConflictError",
]


@dataclass(frozen=True)
class ReassignSettings:
    enabled: bool = DEFAULT_ENABLED
    after_minutes: int = DEFAULT_AFTER_MINUTES
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    eligible_statuses: Tuple[str, ...] = DEFAULT_ELIGIBLE_STATUSES
    max_age_minutes: int = DEFAULT_MAX_AGE_MINUTES
    max_per_run: int = DEFAULT_MAX_PER_RUN
    # Where each field's value came from: "firestore" | "env" | "default".
    sources: Dict[str, str] = field(default_factory=dict, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data.pop("sources", None)
        data["eligible_statuses"] = list(self.eligible_statuses)
        return data


# --- parsing ------------------------------------------------------------------


def _parse_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    return None


def _parse_int(value: Any, *, minimum: int) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= minimum else None


def _parse_statuses(value: Any) -> Optional[Tuple[str, ...]]:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        return None
    statuses = tuple(dict.fromkeys(item.strip().upper() for item in items if str(item).strip()))
    return statuses or None


_PARSERS = {
    "enabled": _parse_bool,
    "after_minutes": lambda v: _parse_int(v, minimum=1),
    "max_attempts": lambda v: _parse_int(v, minimum=0),
    "eligible_statuses": _parse_statuses,
    "max_age_minutes": lambda v: _parse_int(v, minimum=1),
    "max_per_run": lambda v: _parse_int(v, minimum=1),
}

_ENV_NAMES = {
    "enabled": "REASSIGN_ENABLED",
    "after_minutes": "REASSIGN_AFTER_MINUTES",
    "max_attempts": "REASSIGN_MAX_ATTEMPTS",
    "eligible_statuses": "REASSIGN_ELIGIBLE_STATUSES",
    "max_age_minutes": "REASSIGN_MAX_AGE_MINUTES",
    "max_per_run": "REASSIGN_MAX_PER_RUN",
}

_DEFAULTS = {
    "enabled": DEFAULT_ENABLED,
    "after_minutes": DEFAULT_AFTER_MINUTES,
    "max_attempts": DEFAULT_MAX_ATTEMPTS,
    "eligible_statuses": DEFAULT_ELIGIBLE_STATUSES,
    "max_age_minutes": DEFAULT_MAX_AGE_MINUTES,
    "max_per_run": DEFAULT_MAX_PER_RUN,
}


def _resolve(doc: Optional[Dict[str, Any]]) -> ReassignSettings:
    values: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    for name in FIELDS:
        parser = _PARSERS[name]
        if doc is not None and name in doc:
            parsed = parser(doc.get(name))
            if parsed is not None:
                values[name], sources[name] = parsed, "firestore"
                continue
            log_event(logger, "reassign_settings_invalid_firestore_value", field=name)
        raw_env = os.getenv(_ENV_NAMES[name], "").strip()
        if raw_env:
            parsed = parser(raw_env)
            if parsed is not None:
                values[name], sources[name] = parsed, "env"
                continue
            log_event(logger, "reassign_settings_invalid_env", name=_ENV_NAMES[name], value=raw_env)
        values[name], sources[name] = _DEFAULTS[name], "default"
    return ReassignSettings(**values, sources=sources)


# --- Firestore access -----------------------------------------------------------


def _uses_firestore() -> bool:
    return os.getenv(AGENTS_SOURCE_ENV, "file").strip().lower() == "firestore"


def _settings_ref(client):
    collection = os.getenv(SETTINGS_COLLECTION_ENV, "app_config").strip() or "app_config"
    doc_id = os.getenv(SETTINGS_DOC_ENV, "reassign_settings").strip() or "reassign_settings"
    return client.collection(collection).document(doc_id)


def _audit_collection(client):
    name = os.getenv(SETTINGS_AUDIT_COLLECTION_ENV, "reassign_settings_audit").strip()
    return client.collection(name or "reassign_settings_audit")


def _load_doc() -> Optional[Dict[str, Any]]:
    """The raw settings doc, or None when it does not exist / Firestore is not
    the config source. Patched in tests."""
    if not _uses_firestore():
        return None
    client, _ = agent_config_admin._get_firestore_client()
    snap = _settings_ref(client).get()
    if not snap.exists:
        return None
    payload = snap.to_dict() or {}
    return payload if isinstance(payload, dict) else None


# --- runtime read (cached) --------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_CACHE: Optional[Tuple[float, ReassignSettings]] = None


def reset_cache() -> None:
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


def load_settings(*, use_cache: bool = True) -> ReassignSettings:
    """Effective settings. On a Firestore read error this falls back to env
    vars/defaults (fail closed: today that means reassign stays off)."""
    global _CACHE
    if use_cache:
        with _CACHE_LOCK:
            if _CACHE is not None and time.monotonic() - _CACHE[0] < CACHE_SECONDS:
                return _CACHE[1]
    try:
        doc = _load_doc()
    except Exception as exc:
        log_event(logger, "reassign_settings_load_failed", error=str(exc))
        doc = None
    settings = _resolve(doc)
    with _CACHE_LOCK:
        _CACHE = (time.monotonic(), settings)
    return settings


# --- admin read / write -------------------------------------------------------------


def _config_view(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    settings = _resolve(doc)
    return {
        "version": int((doc or {}).get("version", 0) or 0),
        "updated_at": (doc or {}).get("updated_at"),
        "updated_by": (doc or {}).get("updated_by"),
        "change_reason": (doc or {}).get("change_reason"),
        "exists": doc is not None,
        "settings": settings.to_dict(),
        "sources": dict(settings.sources),
    }


def get_settings_config() -> Dict[str, Any]:
    return _config_view(_load_doc())


def _validate_changes(changes: Dict[str, Any]) -> Dict[str, Any]:
    unknown = set(changes) - set(FIELDS)
    if unknown:
        raise ValidationError(f"Unknown reassign setting(s): {', '.join(sorted(unknown))}")
    cleaned: Dict[str, Any] = {}
    for name, value in changes.items():
        parsed = _PARSERS[name](value)
        if parsed is None:
            raise ValidationError(f"Invalid value for {name}")
        cleaned[name] = list(parsed) if name == "eligible_statuses" else parsed
    return cleaned


def update_settings(
    *,
    expected_version: int,
    actor: str,
    change_reason: str,
    changes: Dict[str, Any],
) -> Dict[str, Any]:
    """Versioned partial update. Fields left out keep their current effective
    value (Firestore if set, else env/default), and the written doc always
    holds every field so later env changes can't silently alter it."""
    if not change_reason.strip():
        raise ValidationError("change_reason is required")
    if not changes:
        raise ValidationError("No settings to change")
    cleaned = _validate_changes(changes)

    client, firestore = agent_config_admin._get_firestore_client()
    settings_ref = _settings_ref(client)
    audit_collection = _audit_collection(client)
    txn = client.transaction()

    @firestore.transactional
    def _update(transaction):
        snap = settings_ref.get(transaction=transaction)
        current = (snap.to_dict() or {}) if snap.exists else None
        current_version = int((current or {}).get("version", 0) or 0)
        if current_version != expected_version:
            raise VersionConflictError(
                f"version conflict: expected {expected_version}, got {current_version}"
            )

        merged = _resolve(current).to_dict()
        merged.update(cleaned)
        if merged["max_age_minutes"] < merged["after_minutes"]:
            raise ValidationError("max_age_minutes must be >= after_minutes")

        now_iso = datetime.now(timezone.utc).isoformat()
        updated = {
            **merged,
            "version": current_version + 1,
            "updated_at": now_iso,
            "updated_by": actor,
            "change_reason": change_reason,
        }
        transaction.set(settings_ref, updated)
        transaction.set(
            audit_collection.document(),
            {
                "timestamp": now_iso,
                "actor": actor,
                "change_reason": change_reason,
                "operation": "update_reassign_settings",
                "expected_version": expected_version,
                "before_version": current_version,
                "after_version": current_version + 1,
                "changes": deepcopy(cleaned),
                "before": deepcopy(current),
                "after": deepcopy(updated),
            },
        )
        return updated

    updated = _update(txn)
    reset_cache()
    return _config_view(updated)
