from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from datetime import timedelta
from typing import Dict, Optional, List, Tuple
from zoneinfo import ZoneInfo

from src.api.utils.logging import get_logger, log_event

logger = get_logger(__name__)


TIMEZONE = "Asia/Manila"
STATE_ENV = "AUTO_ASSIGN_STATE_PATH"
STORE_ENV = "AUTO_ASSIGN_STORE"
FULL_POOL_KEY = "full_pool"
# Rotation cursor for the overflow group (group B), kept apart from full_pool.
OVERFLOW_POOL_KEY = "overflow"

ASSIGNMENTS_COLLECTION_ENV = "FIRESTORE_COLLECTION_ASSIGNMENTS"
DAILY_COLLECTION_ENV = "FIRESTORE_COLLECTION_DAILY"
LEASE_COLLECTION_ENV = "FIRESTORE_COLLECTION_REASSIGN_LEASE"  # default: reassign_lease
LEASE_DOC_ID = "sweep"

# Status written on an assignment record the sweep must never move again
# (an agent other than the one we assigned now owns it in LiveAgent).
RELEASED_STATUS = "released_from_reassign"
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

    def get_daily_state(self, date_key: str, pool_key: str = FULL_POOL_KEY) -> Dict[str, object]:
        raise NotImplementedError

    def commit_assignment(
        self,
        *,
        conv_code: str,
        agent_id: str,
        reason: str,
        date_key: str,
        next_index: Optional[int],
        max_assignments: Optional[int],
        pool_key: str = FULL_POOL_KEY,
        now: Optional[datetime] = None,
        extra_cursors: Optional[Dict[str, int]] = None,
    ) -> Dict[str, object]:
        raise NotImplementedError

    def list_stale_assignments(
        self,
        *,
        cutoff: datetime,
        max_reassign_count: int,
        statuses: List[str],
        not_before: Optional[datetime] = None,
    ) -> List[Dict[str, object]]:
        """Bot-made assignments (status "assigned") whose last action is in
        [not_before, cutoff], that have been reassigned fewer than
        max_reassign_count times, and whose last known ticket_status (if
        recorded) is still eligible. Oldest first."""
        raise NotImplementedError

    def mark_ticket_status(self, conv_code: str, ticket_status: Optional[str]) -> None:
        """Remember the last LiveAgent status seen for a ticket so later
        reassign scans can skip it without another LiveAgent call."""
        raise NotImplementedError

    def release_from_reassign(
        self, conv_code: str, *, reason: str, observed_agent_id: Optional[str]
    ) -> None:
        """Take a ticket out of auto-reassign for good (e.g. a person moved it)."""
        raise NotImplementedError

    def try_acquire_reassign_lease(
        self,
        *,
        holder: str,
        now: datetime,
        min_interval_seconds: int,
        ttl_seconds: int,
    ) -> Dict[str, object]:
        """Atomically claim the right to run one reassign sweep. Returns
        {"acquired": bool, "next_allowed_at": datetime}."""
        raise NotImplementedError

    def release_reassign_lease(self, *, holder: str, now: datetime) -> None:
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
        max_assignments: Optional[int],
        pool_key: str = FULL_POOL_KEY,
        now: Optional[datetime] = None,
        extra_cursors: Optional[Dict[str, int]] = None,
    ) -> Dict[str, object]:
        raise NotImplementedError


def _as_datetime(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _is_reassign_candidate(
    record: Dict[str, object],
    *,
    cutoff: datetime,
    max_reassign_count: int,
    statuses: List[str],
    not_before: Optional[datetime],
) -> bool:
    # Only tickets this service assigned. "already_assigned" records are tickets a
    # human (or LiveAgent) had already assigned - never bounce those.
    if record.get("status") != "assigned":
        return False
    ticket_status = record.get("ticket_status")
    if ticket_status is not None and statuses and str(ticket_status) not in statuses:
        return False
    if int(record.get("reassign_count", 0) or 0) >= max_reassign_count:
        return False
    ts = _as_datetime(
        record.get("last_action_at") or record.get("last_reassign_at") or record.get("assigned_at")
    )
    if ts is None:
        return False
    if ts > cutoff:
        return False
    if not_before is not None and ts < not_before:
        return False
    return True


def lease_decision(
    state: Optional[Dict[str, object]],
    *,
    holder: str,
    now: datetime,
    min_interval_seconds: int,
    ttl_seconds: int,
) -> Tuple[bool, Optional[Dict[str, object]], datetime]:
    """Pure lease rule shared by both stores.

    A sweep may start only if (a) at least `min_interval_seconds` have passed
    since the last sweep STARTED anywhere, and (b) no other sweep still holds
    an unexpired lease. The TTL bounds how long a stuck/throttled sweep can
    block others. Returns (acquired, new_state_to_write, next_check_at), where
    next_check_at is when the caller should next bother asking (a hint for the
    per-instance gate; the lease itself is always the source of truth)."""
    state = state or {}
    last_started = _as_datetime(state.get("last_started_at"))
    lease_until = _as_datetime(state.get("lease_until"))
    current_holder = state.get("holder")
    interval = timedelta(seconds=min_interval_seconds)

    interval_ready = last_started + interval if last_started else None
    if interval_ready is not None and now < interval_ready:
        return False, None, interval_ready
    if current_holder and lease_until is not None and now < lease_until:
        # Another sweep is still running; it usually releases well before its
        # TTL, so re-check after one interval rather than waiting out the TTL.
        return False, None, min(lease_until, now + interval)

    new_state = {
        "holder": holder,
        "last_started_at": now,
        "lease_until": now + timedelta(seconds=ttl_seconds),
    }
    return True, new_state, now + timedelta(seconds=min_interval_seconds)


def _sort_oldest_first(records: List[Dict[str, object]]) -> List[Dict[str, object]]:
    def _key(record: Dict[str, object]):
        ts = _as_datetime(record.get("last_action_at") or record.get("assigned_at"))
        return ts.timestamp() if ts else 0.0

    return sorted(records, key=_key)


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

    def _pool_cursor(self, day_state: Dict[str, object], pool_key: str) -> Dict[str, object]:
        if pool_key == FULL_POOL_KEY:
            return day_state
        pools = day_state.setdefault("pools", {})
        return pools.setdefault(pool_key, {"last_index": -1})

    def get_daily_state(self, date_key: str, pool_key: str = FULL_POOL_KEY) -> Dict[str, object]:
        with self._lock:
            state = self._load_state()
            daily = state.setdefault("daily", {})
            day_state = daily.setdefault(date_key, {"last_index": -1, "counts": {}})
            cursor = self._pool_cursor(day_state, pool_key)
            return {
                "last_index": int(cursor.get("last_index", -1)),
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
        max_assignments: Optional[int],
        pool_key: str = FULL_POOL_KEY,
        now: Optional[datetime] = None,
        extra_cursors: Optional[Dict[str, int]] = None,
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
            if max_assignments is not None and current_count >= max_assignments:
                return {
                    "status": "no_eligible_agents",
                    "conv_code": conv_code,
                    "agent_id": None,
                    "reason": "quota_reached",
                }
            # Every assignment counts toward the agent's single daily total, even
            # when the cap is not enforced (direct / owner_override).
            counts[agent_id] = current_count + 1

            if next_index is not None:
                cursor = self._pool_cursor(day_state, pool_key)
                cursor["last_index"] = next_index
                cursor["last_agent_id"] = agent_id
            for extra_key, extra_index in (extra_cursors or {}).items():
                extra_cursor = self._pool_cursor(day_state, extra_key)
                extra_cursor["last_index"] = int(extra_index)
                extra_cursor["last_agent_id"] = agent_id

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
        not_before: Optional[datetime] = None,
    ) -> List[Dict[str, object]]:
        with self._lock:
            state = self._load_state()
            assignments = state.get("assignments") or {}
            results = [
                record
                for record in assignments.values()
                if _is_reassign_candidate(
                    record,
                    cutoff=cutoff,
                    max_reassign_count=max_reassign_count,
                    statuses=statuses,
                    not_before=not_before,
                )
            ]
            return _sort_oldest_first(results)

    def mark_ticket_status(self, conv_code: str, ticket_status: Optional[str]) -> None:
        with self._lock:
            state = self._load_state()
            record = (state.get("assignments") or {}).get(conv_code)
            if not record:
                return
            record["ticket_status"] = ticket_status
            self._save_state(state)

    def release_from_reassign(
        self, conv_code: str, *, reason: str, observed_agent_id: Optional[str]
    ) -> None:
        with self._lock:
            state = self._load_state()
            record = (state.get("assignments") or {}).get(conv_code)
            if not record:
                return
            record["status"] = RELEASED_STATUS
            record["release_reason"] = reason
            record["observed_agent_id"] = observed_agent_id
            self._save_state(state)

    def try_acquire_reassign_lease(
        self,
        *,
        holder: str,
        now: datetime,
        min_interval_seconds: int,
        ttl_seconds: int,
    ) -> Dict[str, object]:
        with self._lock:
            state = self._load_state()
            acquired, new_state, next_allowed = lease_decision(
                state.get("reassign_lease"),
                holder=holder,
                now=now,
                min_interval_seconds=min_interval_seconds,
                ttl_seconds=ttl_seconds,
            )
            if acquired and new_state is not None:
                state["reassign_lease"] = {
                    key: (value.isoformat() if isinstance(value, datetime) else value)
                    for key, value in new_state.items()
                }
                self._save_state(state)
            return {"acquired": acquired, "next_allowed_at": next_allowed}

    def release_reassign_lease(self, *, holder: str, now: datetime) -> None:
        with self._lock:
            state = self._load_state()
            lease = state.get("reassign_lease") or {}
            if lease.get("holder") != holder:
                return
            lease["holder"] = None
            lease["lease_until"] = None
            lease["released_at"] = now.isoformat()
            state["reassign_lease"] = lease
            self._save_state(state)

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
        max_assignments: Optional[int],
        pool_key: str = FULL_POOL_KEY,
        now: Optional[datetime] = None,
        extra_cursors: Optional[Dict[str, int]] = None,
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

            reassign_count = int(existing.get("reassign_count", 0) or 0) + 1
            if reassign_count > max_reassign_count:
                return {
                    "status": "no_eligible_agents",
                    "conv_code": conv_code,
                    "agent_id": None,
                    "reason": "reassign_limit_reached",
                }

            daily = state.setdefault("daily", {})
            day_state = daily.setdefault(date_key, {"last_index": -1, "counts": {}})
            counts = day_state.setdefault("counts", {})
            current_count = int(counts.get(new_agent_id, 0))
            if max_assignments is not None and current_count >= max_assignments:
                return {
                    "status": "no_eligible_agents",
                    "conv_code": conv_code,
                    "agent_id": None,
                    "reason": "quota_reached",
                }
            counts[new_agent_id] = current_count + 1

            if next_index is not None:
                cursor = self._pool_cursor(day_state, pool_key)
                cursor["last_index"] = next_index
                cursor["last_agent_id"] = new_agent_id
            for extra_key, extra_index in (extra_cursors or {}).items():
                extra_cursor = self._pool_cursor(day_state, extra_key)
                extra_cursor["last_index"] = int(extra_index)
                extra_cursor["last_agent_id"] = new_agent_id
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


def _daily_updates(
    firestore,
    *,
    date_key: str,
    agent_id: str,
    next_index: Optional[int],
    pool_key: str,
    now: datetime,
    extra_cursors: Optional[Dict[str, int]] = None,
) -> Dict[str, object]:
    """Payload for txn.set(daily_ref, ..., merge=True).

    Must use NESTED dicts. set(merge=True) does not expand dotted keys, so the
    previous {"counts.<id>": Increment(1)} created a literal top-level field
    named "counts.<id>" - the `counts` map stayed empty, the daily cap was never
    enforced, and team-pool cursors ("pools.<key>.last_index") never persisted.
    """
    updates: Dict[str, object] = {
        "date": date_key,
        "updated_at": now.isoformat(),
        # Always counted: one shared daily total per agent across all pools.
        "counts": {agent_id: firestore.Increment(1)},
    }
    if next_index is not None:
        if pool_key == FULL_POOL_KEY:
            updates["last_index"] = int(next_index)
            updates["last_agent_id"] = agent_id
        else:
            updates["pools"] = {
                pool_key: {"last_index": int(next_index), "last_agent_id": agent_id}
            }
    for extra_key, extra_index in (extra_cursors or {}).items():
        pools = updates.setdefault("pools", {})
        pools[extra_key] = {"last_index": int(extra_index), "last_agent_id": agent_id}
    return updates


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

    def get_daily_state(self, date_key: str, pool_key: str = FULL_POOL_KEY) -> Dict[str, object]:
        snap = self._daily_ref(date_key).get()
        if not snap.exists:
            return {"last_index": -1, "counts": {}}
        state = snap.to_dict() or {}
        if pool_key == FULL_POOL_KEY:
            last_index = int(state.get("last_index", -1))
        else:
            pool_state = (state.get("pools") or {}).get(pool_key) or {}
            last_index = int(pool_state.get("last_index", -1))
        return {
            "last_index": last_index,
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
        max_assignments: Optional[int],
        pool_key: str = FULL_POOL_KEY,
        now: Optional[datetime] = None,
        extra_cursors: Optional[Dict[str, int]] = None,
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
            if max_assignments is not None:
                current_count = int(counts.get(agent_id, 0))
                if current_count >= max_assignments:
                    return {
                        "status": "no_eligible_agents",
                        "conv_code": conv_code,
                        "agent_id": None,
                        "reason": "quota_reached",
                    }

            txn.set(
                daily_ref,
                _daily_updates(
                    firestore,
                    date_key=date_key,
                    agent_id=agent_id,
                    next_index=next_index,
                    pool_key=pool_key,
                    now=current,
                    extra_cursors=extra_cursors,
                ),
                merge=True,
            )

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
        not_before: Optional[datetime] = None,
    ) -> List[Dict[str, object]]:
        if max_reassign_count <= 0:
            return []
        # Range on a single field only (auto single-field index; no composite
        # index needed). The lower bound keeps the scan to recent tickets so
        # enabling reassign never sweeps up the whole assignment history.
        query = self._client.collection(self._assignments_collection).where(
            "last_action_at", "<=", cutoff
        )
        if not_before is not None:
            query = query.where("last_action_at", ">=", not_before)
        results = [
            record
            for record in (doc.to_dict() or {} for doc in query.stream())
            if _is_reassign_candidate(
                record,
                cutoff=cutoff,
                max_reassign_count=max_reassign_count,
                statuses=statuses,
                not_before=not_before,
            )
        ]
        return _sort_oldest_first(results)

    def mark_ticket_status(self, conv_code: str, ticket_status: Optional[str]) -> None:
        self._assignment_ref(conv_code).set({"ticket_status": ticket_status}, merge=True)

    def release_from_reassign(
        self, conv_code: str, *, reason: str, observed_agent_id: Optional[str]
    ) -> None:
        self._assignment_ref(conv_code).set(
            {
                "status": RELEASED_STATUS,
                "release_reason": reason,
                "observed_agent_id": observed_agent_id,
            },
            merge=True,
        )

    def _lease_ref(self):
        collection = os.getenv(LEASE_COLLECTION_ENV, "reassign_lease").strip() or "reassign_lease"
        return self._client.collection(collection).document(LEASE_DOC_ID)

    def try_acquire_reassign_lease(
        self,
        *,
        holder: str,
        now: datetime,
        min_interval_seconds: int,
        ttl_seconds: int,
    ) -> Dict[str, object]:
        lease_ref = self._lease_ref()
        transaction = self._client.transaction()
        firestore = self._firestore

        @firestore.transactional
        def _acquire(txn):
            snap = lease_ref.get(transaction=txn)
            acquired, new_state, next_allowed = lease_decision(
                snap.to_dict() if snap.exists else None,
                holder=holder,
                now=now,
                min_interval_seconds=min_interval_seconds,
                ttl_seconds=ttl_seconds,
            )
            if acquired and new_state is not None:
                txn.set(lease_ref, new_state, merge=True)
            return {"acquired": acquired, "next_allowed_at": next_allowed}

        return _acquire(transaction)

    def release_reassign_lease(self, *, holder: str, now: datetime) -> None:
        lease_ref = self._lease_ref()
        transaction = self._client.transaction()
        firestore = self._firestore

        @firestore.transactional
        def _release(txn):
            snap = lease_ref.get(transaction=txn)
            if not snap.exists or (snap.to_dict() or {}).get("holder") != holder:
                return
            txn.set(
                lease_ref,
                {"holder": None, "lease_until": None, "released_at": now},
                merge=True,
            )

        _release(transaction)

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
        max_assignments: Optional[int],
        pool_key: str = FULL_POOL_KEY,
        now: Optional[datetime] = None,
        extra_cursors: Optional[Dict[str, int]] = None,
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
            if max_assignments is not None:
                current_count = int(counts.get(new_agent_id, 0))
                if current_count >= max_assignments:
                    return {
                        "status": "no_eligible_agents",
                        "conv_code": conv_code,
                        "agent_id": None,
                        "reason": "quota_reached",
                    }

            txn.set(
                daily_ref,
                _daily_updates(
                    firestore,
                    date_key=date_key,
                    agent_id=new_agent_id,
                    next_index=next_index,
                    pool_key=pool_key,
                    now=current,
                    extra_cursors=extra_cursors,
                ),
                merge=True,
            )

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
