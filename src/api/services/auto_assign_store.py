from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from datetime import timedelta
from typing import Dict, Optional, List
from zoneinfo import ZoneInfo

from src.api.utils.logging import get_logger, log_event

logger = get_logger(__name__)


TIMEZONE = "Asia/Manila"
STATE_ENV = "AUTO_ASSIGN_STATE_PATH"
STORE_ENV = "AUTO_ASSIGN_STORE"

ASSIGNMENTS_COLLECTION_ENV = "FIRESTORE_COLLECTION_ASSIGNMENTS"
DAILY_COLLECTION_ENV = "FIRESTORE_COLLECTION_DAILY"
FIRESTORE_PROJECT_ENV = "FIRESTORE_PROJECT_ID"
FIRESTORE_DATABASE_ENV = "FIRESTORE_DATABASE_ID"
FIRESTORE_CREDENTIALS_ENV = "CREDENTIALS"


@dataclass(frozen=True)
class AssignmentRecord:
    status: str
    conv_code: str
    agent_id: Optional[str]
    reason: str
    created_at: str


class AutoAssignStore:
    def get_assignment(self, conv_code: str) -> Optional[Dict[str, object]]:
        raise NotImplementedError

    def record_assignment(
        self,
        *,
        conv_code: str,
        agent_id: Optional[str],
        reason: str,
        status: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        raise NotImplementedError

    def get_daily_state(self, date_key: str) -> Dict[str, object]:
        raise NotImplementedError

    def commit_assignment(
        self,
        *,
        conv_code: str,
        agent_id: str,
        reason: str,
        date_key: str,
        next_index: Optional[int],
        max_assignments: int,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        raise NotImplementedError

    def list_stale_assignments(
        self,
        *,
        cutoff: datetime,
        max_reassign_count: int,
        statuses: List[str],
    ) -> List[Dict[str, object]]:
        raise NotImplementedError

    def commit_reassign(
        self,
        *,
        conv_code: str,
        new_agent_id: str,
        previous_agent_id: Optional[str],
        reason: str,
        date_key: str,
        next_index: Optional[int],
        max_reassign_count: int,
        max_assignments: int,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        raise NotImplementedError


def _state_path() -> Path:
    override = os.getenv(STATE_ENV)
    if override:
        return Path(override)
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "data" / "auto_assign_state.json"


class FileAutoAssignStore(AutoAssignStore):
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def _load_state(self) -> Dict[str, object]:
        path = _state_path()
        if not path.exists():
            return {"assignments": {}, "daily": {}}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"assignments": {}, "daily": {}}

    def _save_state(self, state: Dict[str, object]) -> None:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, ensure_ascii=True), encoding="utf-8")

    def get_assignment(self, conv_code: str) -> Optional[Dict[str, object]]:
        with self._lock:
            state = self._load_state()
            return (state.get("assignments") or {}).get(conv_code)

    def record_assignment(
        self,
        *,
        conv_code: str,
        agent_id: Optional[str],
        reason: str,
        status: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        current = now or datetime.now(ZoneInfo(TIMEZONE))
        record = {
            "status": status,
            "conv_code": conv_code,
            "agent_id": agent_id,
            "reason": reason,
            "created_at": current.isoformat(),
            "assigned_at": current.isoformat(),
            "reassign_count": 0,
            "last_reassign_at": None,
            "last_action_at": current.isoformat(),
        }
        with self._lock:
            state = self._load_state()
            assignments = state.setdefault("assignments", {})
            assignments[conv_code] = record
            self._save_state(state)
        return record

    def get_daily_state(self, date_key: str) -> Dict[str, object]:
        with self._lock:
            state = self._load_state()
            daily = state.setdefault("daily", {})
            day_state = daily.setdefault(date_key, {"last_index": -1, "counts": {}})
            return {
                "last_index": int(day_state.get("last_index", -1)),
                "counts": dict(day_state.get("counts") or {}),
            }

    def commit_assignment(
        self,
        *,
        conv_code: str,
        agent_id: str,
        reason: str,
        date_key: str,
        next_index: Optional[int],
        max_assignments: int,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        current = now or datetime.now(ZoneInfo(TIMEZONE))
        with self._lock:
            state = self._load_state()
            assignments = state.setdefault("assignments", {})
            existing = assignments.get(conv_code)
            if existing:
                return {
                    "status": "already_assigned",
                    "conv_code": conv_code,
                    "agent_id": existing.get("agent_id"),
                    "reason": "idempotent_replay",
                }

            daily = state.setdefault("daily", {})
            day_state = daily.setdefault(date_key, {"last_index": -1, "counts": {}})
            counts = day_state.setdefault("counts", {})
            current_count = int(counts.get(agent_id, 0))
            if current_count >= max_assignments:
                return {
                    "status": "no_eligible_agents",
                    "conv_code": conv_code,
                    "agent_id": None,
                    "reason": "quota_reached",
                }

            counts[agent_id] = current_count + 1
            if next_index is not None:
                day_state["last_index"] = next_index
                day_state["last_agent_id"] = agent_id

            record = {
                "status": "assigned",
                "conv_code": conv_code,
                "agent_id": agent_id,
                "reason": reason,
                "created_at": current.isoformat(),
                "assigned_at": current.isoformat(),
                "reassign_count": 0,
                "last_reassign_at": None,
                "last_action_at": current.isoformat(),
            }
            assignments[conv_code] = record
            self._save_state(state)
            return {
                "status": "assigned",
                "conv_code": conv_code,
                "agent_id": agent_id,
                "reason": reason,
            }

    def list_stale_assignments(
        self,
        *,
        cutoff: datetime,
        max_reassign_count: int,
        statuses: List[str],
    ) -> List[Dict[str, object]]:
        with self._lock:
            state = self._load_state()
            assignments = state.get("assignments") or {}
            results: List[Dict[str, object]] = []
            for record in assignments.values():
                ticket_status = record.get("ticket_status")
                if ticket_status is not None:
                    status = str(ticket_status or "")
                    if status not in statuses:
                        continue
                reassign_count = int(record.get("reassign_count", 0) or 0)
                if reassign_count >= max_reassign_count:
                    continue
                ts_raw = record.get("last_action_at") or record.get("last_reassign_at") or record.get("assigned_at")
                if not ts_raw:
                    continue
                try:
                    ts = datetime.fromisoformat(str(ts_raw))
                except Exception:
                    continue
                if ts <= cutoff:
                    results.append(record)
            return results

    def commit_reassign(
        self,
        *,
        conv_code: str,
        new_agent_id: str,
        previous_agent_id: Optional[str],
        reason: str,
        date_key: str,
        next_index: Optional[int],
        max_reassign_count: int,
        max_assignments: int,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        current = now or datetime.now(ZoneInfo(TIMEZONE))
        with self._lock:
            state = self._load_state()
            assignments = state.setdefault("assignments", {})
            existing = assignments.get(conv_code)
            if not existing:
                return {
                    "status": "missing_assignment",
                    "conv_code": conv_code,
                    "agent_id": None,
                    "reason": "missing_assignment",
                }

            daily = state.setdefault("daily", {})
            day_state = daily.setdefault(date_key, {"last_index": -1, "counts": {}})
            counts = day_state.setdefault("counts", {})
            current_count = int(counts.get(new_agent_id, 0))
            if current_count >= max_assignments:
                return {
                    "status": "no_eligible_agents",
                    "conv_code": conv_code,
                    "agent_id": None,
                    "reason": "quota_reached",
                }

            counts[new_agent_id] = current_count + 1
            if next_index is not None:
                day_state["last_index"] = next_index
                day_state["last_agent_id"] = new_agent_id

            reassign_count = int(existing.get("reassign_count", 0) or 0) + 1
            if reassign_count > max_reassign_count:
                return {
                    "status": "no_eligible_agents",
                    "conv_code": conv_code,
                    "agent_id": None,
                    "reason": "reassign_limit_reached",
                }
            record = {
                **existing,
                "status": "assigned",
                "conv_code": conv_code,
                "agent_id": new_agent_id,
                "previous_agent_id": previous_agent_id,
                "reason": reason,
                "reassign_count": reassign_count,
                "last_reassign_at": current.isoformat(),
                "updated_at": current.isoformat(),
                "last_action_at": current.isoformat(),
            }
            assignments[conv_code] = record
            self._save_state(state)
            return {
                "status": "reassigned",
                "conv_code": conv_code,
                "agent_id": new_agent_id,
                "reason": reason,
            }


class FirestoreAutoAssignStore(AutoAssignStore):
    def __init__(self) -> None:
        try:
            from google.cloud import firestore
            from google.oauth2 import service_account
        except ImportError as exc:
            raise RuntimeError("google-cloud-firestore is required for Firestore persistence") from exc

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

        database_id = os.getenv(FIRESTORE_DATABASE_ENV, "(default)").strip()
        self._client = firestore.Client(
            project=project_id or None,
            credentials=credentials,
            database=database_id if database_id else "(default)",
        )
        self._firestore = firestore

        self._assignments_collection = os.getenv(ASSIGNMENTS_COLLECTION_ENV, "assignments")
        self._daily_collection = os.getenv(DAILY_COLLECTION_ENV, "daily_state")

    def _assignment_ref(self, conv_code: str):
        return self._client.collection(self._assignments_collection).document(conv_code)

    def _daily_ref(self, date_key: str):
        return self._client.collection(self._daily_collection).document(date_key)

    def get_assignment(self, conv_code: str) -> Optional[Dict[str, object]]:
        snap = self._assignment_ref(conv_code).get()
        return snap.to_dict() if snap.exists else None

    def record_assignment(
        self,
        *,
        conv_code: str,
        agent_id: Optional[str],
        reason: str,
        status: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        current = now or datetime.now(ZoneInfo(TIMEZONE))
        record = {
            "status": status,
            "conv_code": conv_code,
            "agent_id": agent_id,
            "reason": reason,
            "created_at": current.isoformat(),
            "assigned_at": current,
            "reassign_count": 0,
            "last_reassign_at": None,
            "last_action_at": current,
        }
        self._assignment_ref(conv_code).set(record)
        return record

    def get_daily_state(self, date_key: str) -> Dict[str, object]:
        snap = self._daily_ref(date_key).get()
        if not snap.exists:
            return {"last_index": -1, "counts": {}}
        state = snap.to_dict() or {}
        return {
            "last_index": int(state.get("last_index", -1)),
            "counts": dict(state.get("counts") or {}),
        }

    def commit_assignment(
        self,
        *,
        conv_code: str,
        agent_id: str,
        reason: str,
        date_key: str,
        next_index: Optional[int],
        max_assignments: int,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        current = now or datetime.now(ZoneInfo(TIMEZONE))
        assignment_ref = self._assignment_ref(conv_code)
        daily_ref = self._daily_ref(date_key)

        transaction = self._client.transaction()
        firestore = self._firestore

        @firestore.transactional
        def _commit(txn):
            assignment_snap = assignment_ref.get(transaction=txn)
            if assignment_snap.exists:
                existing = assignment_snap.to_dict() or {}
                return {
                    "status": "already_assigned",
                    "conv_code": conv_code,
                    "agent_id": existing.get("agent_id"),
                    "reason": "idempotent_replay",
                }

            day_snap = daily_ref.get(transaction=txn)
            day_state = day_snap.to_dict() if day_snap.exists else {}
            counts = dict(day_state.get("counts") or {})
            current_count = int(counts.get(agent_id, 0))
            if current_count >= max_assignments:
                return {
                    "status": "no_eligible_agents",
                    "conv_code": conv_code,
                    "agent_id": None,
                    "reason": "quota_reached",
                }

            updates = {
                "date": date_key,
                "updated_at": current.isoformat(),
                f"counts.{agent_id}": firestore.Increment(1),
            }
            if next_index is not None:
                updates["last_index"] = int(next_index)
                updates["last_agent_id"] = agent_id

            txn.set(daily_ref, updates, merge=True)

            record = {
                "status": "assigned",
                "conv_code": conv_code,
                "agent_id": agent_id,
                "reason": reason,
                "created_at": current.isoformat(),
                "assigned_at": current,
                "reassign_count": 0,
                "last_reassign_at": None,
                "last_action_at": current,
            }
            txn.set(assignment_ref, record)
            return {
                "status": "assigned",
                "conv_code": conv_code,
                "agent_id": agent_id,
                "reason": reason,
            }

        return _commit(transaction)

    def list_stale_assignments(
        self,
        *,
        cutoff: datetime,
        max_reassign_count: int,
        statuses: List[str],
    ) -> List[Dict[str, object]]:
        query = (
            self._client.collection(self._assignments_collection)
            .where("reassign_count", "<", max_reassign_count)
            .where("last_action_at", "<=", cutoff)
        )
        results = [doc.to_dict() for doc in query.stream()]
        if not statuses:
            return results
        filtered = []
        for record in results:
            ticket_status = record.get("ticket_status")
            if ticket_status is None:
                filtered.append(record)
                continue
            status = str(ticket_status or "")
            if status in statuses:
                filtered.append(record)
        return filtered

    def commit_reassign(
        self,
        *,
        conv_code: str,
        new_agent_id: str,
        previous_agent_id: Optional[str],
        reason: str,
        date_key: str,
        next_index: Optional[int],
        max_reassign_count: int,
        max_assignments: int,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        current = now or datetime.now(ZoneInfo(TIMEZONE))
        assignment_ref = self._assignment_ref(conv_code)
        daily_ref = self._daily_ref(date_key)

        transaction = self._client.transaction()
        firestore = self._firestore

        @firestore.transactional
        def _commit(txn):
            assignment_snap = assignment_ref.get(transaction=txn)
            if not assignment_snap.exists:
                return {
                    "status": "missing_assignment",
                    "conv_code": conv_code,
                    "agent_id": None,
                    "reason": "missing_assignment",
                }
            existing = assignment_snap.to_dict() or {}
            reassign_count = int(existing.get("reassign_count", 0) or 0)
            if reassign_count >= max_reassign_count:
                return {
                    "status": "no_eligible_agents",
                    "conv_code": conv_code,
                    "agent_id": None,
                    "reason": "reassign_limit_reached",
                }

            day_snap = daily_ref.get(transaction=txn)
            day_state = day_snap.to_dict() if day_snap.exists else {}
            counts = dict(day_state.get("counts") or {})
            current_count = int(counts.get(new_agent_id, 0))
            if current_count >= max_assignments:
                return {
                    "status": "no_eligible_agents",
                    "conv_code": conv_code,
                    "agent_id": None,
                    "reason": "quota_reached",
                }

            updates = {
                "date": date_key,
                "updated_at": current.isoformat(),
                f"counts.{new_agent_id}": firestore.Increment(1),
            }
            if next_index is not None:
                updates["last_index"] = int(next_index)
                updates["last_agent_id"] = new_agent_id

            txn.set(daily_ref, updates, merge=True)

            record = {
                **existing,
                "status": "assigned",
                "agent_id": new_agent_id,
                "previous_agent_id": previous_agent_id,
                "reason": reason,
                "reassign_count": reassign_count + 1,
                "last_reassign_at": current,
                "updated_at": current.isoformat(),
                "last_action_at": current,
            }
            txn.set(assignment_ref, record)
            return {
                "status": "reassigned",
                "conv_code": conv_code,
                "agent_id": new_agent_id,
                "reason": reason,
            }

        return _commit(transaction)


_STORE_CACHE: Optional[AutoAssignStore] = None


def get_store() -> AutoAssignStore:
    global _STORE_CACHE
    if _STORE_CACHE is None:
        backend = os.getenv(STORE_ENV, "firestore").strip().lower()
        if backend == "file":
            log_event(logger, "auto_assign_store_file")
            _STORE_CACHE = FileAutoAssignStore()
        else:
            log_event(logger, "auto_assign_store_firestore")
            _STORE_CACHE = FirestoreAutoAssignStore()
    return _STORE_CACHE


def reset_store_cache() -> None:
    global _STORE_CACHE
    _STORE_CACHE = None
