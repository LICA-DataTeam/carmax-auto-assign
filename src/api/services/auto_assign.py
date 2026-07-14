from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from src.api.utils.logging import get_logger, log_event
from src.api.services.auto_assign_store import FULL_POOL_KEY, get_store


MAX_ASSIGNMENTS_PER_AGENT = 30
TIMEZONE = "Asia/Manila"
WINDOW_START_HOUR = 8
WINDOW_START_MINUTE = 30
WINDOW_END_HOUR = 17
WINDOW_END_MINUTE = 30
CONFIG_ENV = "AUTO_ASSIGN_CONFIG_PATH"
logger = get_logger(__name__)

AGENTS_SOURCE_ENV = "AGENTS_SOURCE"
AGENTS_FIRESTORE_COLLECTION_ENV = "FIRESTORE_COLLECTION_AGENT_CONFIG"  # default: app_config
AGENTS_FIRESTORE_DOC_ENV = "FIRESTORE_DOC_AGENT_CONFIG"  # default: carmax_agents
FIRESTORE_PROJECT_ENV = "FIRESTORE_PROJECT_ID"
FIRESTORE_DATABASE_ENV = "FIRESTORE_DATABASE_ID"
FIRESTORE_CREDENTIALS_ENV = "CREDENTIALS"

DEPARTMENT_ROUTING_CONFIG_ENV = "DEPARTMENT_ROUTING_CONFIG_PATH"
DEPARTMENT_ROUTING_DOC_ENV = "FIRESTORE_DOC_DEPARTMENT_ROUTING"  # default: department_routing
DEPARTMENT_ROUTING_AUDIT_COLLECTION_ENV = "FIRESTORE_COLLECTION_DEPARTMENT_ROUTING_AUDIT"  # default: department_routing_audit

DEFAULT_DEPARTMENT_ID = "default"
ADMIN_LOGIN_AGENT_KEY = "1h7uz719"

ROUTING_MODE_DIRECT = "direct"
ROUTING_MODE_TEAM_POOL = "team_pool"
ROUTING_MODE_FULL_POOL = "full_pool"
VALID_ROUTING_MODES = {ROUTING_MODE_DIRECT, ROUTING_MODE_TEAM_POOL, ROUTING_MODE_FULL_POOL}


@dataclass(frozen=True)
class Agent:
    agent_id: str
    agent_name: str
    active: bool


@dataclass(frozen=True)
class DepartmentRoute:
    department_id: str
    label: str
    mode: str
    agent_keys: Tuple[str, ...]
    active: bool
    owner_overrides: Dict[str, str] = field(default_factory=dict)  # normalized owner_name -> agent_key


def _normalize_owner_name(value: object) -> str:
    return str(value or "").strip().lower()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _config_path() -> Path:
    override = os.getenv(CONFIG_ENV)
    if override:
        return Path(override)
    return _repo_root() / "config" / "carmax_agents.json"


def _parse_active_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return bool(value)

def _load_agents_payload_from_file() -> Dict[str, object]:
    path = _config_path()
    if not path.exists():
        raise FileNotFoundError(f"Agent config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))

def _load_agents_payload_from_firestore() -> Dict[str, object]:
    try:
        from google.cloud import firestore
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError("google-cloud-firestore is required for Firestore agent config") from exc

    creds_raw = os.getenv(FIRESTORE_CREDENTIALS_ENV, "").strip()
    credentials = None
    project_id = os.getenv(FIRESTORE_PROJECT_ENV, "").strip() or None

    if creds_raw:
        info = None
        if creds_raw.startswith("{"):
            info = json.loads(creds_raw)
        else:
            creds_path = Path(creds_raw)
            if creds_path.exists():
                info = json.loads(creds_path.read_text(encoding="utf-8"))
        if info:
            credentials = service_account.Credentials.from_service_account_info(info)
            project_id = project_id or info.get("project_id")

    database_id = os.getenv(FIRESTORE_DATABASE_ENV, "(default)").strip() or "(default)"
    client = firestore.Client(
        project=project_id or None,
        credentials=credentials,
        database=database_id,
    )

    collection_name = os.getenv(AGENTS_FIRESTORE_COLLECTION_ENV, "app_config").strip() or "app_config"
    doc_id = os.getenv(AGENTS_FIRESTORE_DOC_ENV, "carmax_agents").strip() or "carmax_agents"

    snap = client.collection(collection_name).document(doc_id).get()
    if not snap.exists:
        raise FileNotFoundError(f"Firestore agent config not found: {collection_name}/{doc_id}")

    payload = snap.to_dict() or {}
    if not isinstance(payload, dict):
        raise ValueError("Firestore agent config document must be an object")
    return payload


def _parse_agents_payload(payload: Dict[str, object], *, source: str) -> List[Agent]:
    teams = payload.get("teams") or []
    if not isinstance(teams, list):
        raise ValueError("Agent config 'teams' must be a list")

    agents: List[Agent] = []
    seen_agent_ids: set[str] = set()

    for team_idx, team in enumerate(teams):
        if not isinstance(team, dict):
            raise ValueError(f"Team entry at index {team_idx} must be an object")

        team_agents = team.get("agents") or []
        if not isinstance(team_agents, list):
            raise ValueError(f"Team entry at index {team_idx} has invalid 'agents' list")

        for agent_idx, item in enumerate(team_agents):
            if not isinstance(item, dict):
                raise ValueError(
                    f"Agent entry at team index {team_idx}, agent index {agent_idx} must be an object"
                )

            agent_id = str(item.get("agent_key") or "").strip()
            if not agent_id:
                raise ValueError(
                    f"Missing required 'agent_key' at team index {team_idx}, agent index {agent_idx}"
                )
            if agent_id in seen_agent_ids:
                raise ValueError(f"Duplicate agent_key found: {agent_id}")
            seen_agent_ids.add(agent_id)

            agent_name = str(item.get("agent_name") or "").strip()
            if not agent_name:
                raise ValueError(
                    f"Missing required 'agent_name' for agent_key '{agent_id}'"
                )

            agents.append(
                Agent(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    active=_parse_active_flag(item.get("active", True)),
                )
            )

    if not agents:
        log_event(logger, "auto_assign_agents_empty", source=source)
    return agents


def load_agents() -> List[Agent]:
    source = os.getenv(AGENTS_SOURCE_ENV, "file").strip().lower()

    if source == "firestore":
        payload = _load_agents_payload_from_firestore()
    else:
        payload = _load_agents_payload_from_file()

    return _parse_agents_payload(payload, source=source)


def _department_routing_config_path() -> Path:
    override = os.getenv(DEPARTMENT_ROUTING_CONFIG_ENV)
    if override:
        return Path(override)
    return _repo_root() / "config" / "department_routing.json"


def _load_department_routing_payload_from_file() -> Dict[str, object]:
    path = _department_routing_config_path()
    if not path.exists():
        raise FileNotFoundError(f"Department routing config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_department_routing_payload_from_firestore() -> Dict[str, object]:
    try:
        from google.cloud import firestore
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError("google-cloud-firestore is required for Firestore department routing config") from exc

    creds_raw = os.getenv(FIRESTORE_CREDENTIALS_ENV, "").strip()
    credentials = None
    project_id = os.getenv(FIRESTORE_PROJECT_ENV, "").strip() or None

    if creds_raw:
        info = None
        if creds_raw.startswith("{"):
            info = json.loads(creds_raw)
        else:
            creds_path = Path(creds_raw)
            if creds_path.exists():
                info = json.loads(creds_path.read_text(encoding="utf-8"))
        if info:
            credentials = service_account.Credentials.from_service_account_info(info)
            project_id = project_id or info.get("project_id")

    database_id = os.getenv(FIRESTORE_DATABASE_ENV, "(default)").strip() or "(default)"
    client = firestore.Client(
        project=project_id or None,
        credentials=credentials,
        database=database_id,
    )

    collection_name = os.getenv(AGENTS_FIRESTORE_COLLECTION_ENV, "app_config").strip() or "app_config"
    doc_id = os.getenv(DEPARTMENT_ROUTING_DOC_ENV, "department_routing").strip() or "department_routing"

    snap = client.collection(collection_name).document(doc_id).get()
    if not snap.exists:
        raise FileNotFoundError(f"Firestore department routing config not found: {collection_name}/{doc_id}")

    payload = snap.to_dict() or {}
    if not isinstance(payload, dict):
        raise ValueError("Firestore department routing config document must be an object")
    return payload


def _parse_department_routing_payload(payload: Dict[str, object], *, source: str) -> Dict[str, DepartmentRoute]:
    departments = payload.get("departments") or {}
    if not isinstance(departments, dict):
        raise ValueError("Department routing 'departments' must be an object")

    routes: Dict[str, DepartmentRoute] = {}
    for raw_department_id, raw in departments.items():
        department_id = str(raw_department_id).strip()
        if not department_id:
            raise ValueError("Department id cannot be empty")
        if department_id == DEFAULT_DEPARTMENT_ID:
            raise ValueError("Department id 'default' is reserved and cannot be routed")
        if not isinstance(raw, dict):
            raise ValueError(f"Department entry for '{department_id}' must be an object")

        mode = str(raw.get("mode") or "").strip().lower()
        if mode not in VALID_ROUTING_MODES:
            raise ValueError(f"Invalid mode '{mode}' for department '{department_id}'")

        agent_keys_raw = raw.get("agent_keys") or []
        if not isinstance(agent_keys_raw, list):
            raise ValueError(f"Department '{department_id}' 'agent_keys' must be a list")
        agent_keys = tuple(str(key).strip() for key in agent_keys_raw if str(key).strip())
        if ADMIN_LOGIN_AGENT_KEY in agent_keys:
            raise ValueError(
                f"agent_key '{ADMIN_LOGIN_AGENT_KEY}' is the shared admin login and cannot be assignable"
            )

        active = _parse_active_flag(raw.get("active", True))
        if active and mode in (ROUTING_MODE_DIRECT, ROUTING_MODE_TEAM_POOL):
            if not agent_keys:
                raise ValueError(f"Department '{department_id}' requires at least one agent_key")
            if mode == ROUTING_MODE_DIRECT and len(agent_keys) != 1:
                raise ValueError(f"Department '{department_id}' mode 'direct' requires exactly one agent_key")

        owner_overrides_raw = raw.get("owner_overrides") or {}
        if not isinstance(owner_overrides_raw, dict):
            raise ValueError(f"Department '{department_id}' 'owner_overrides' must be an object")
        owner_overrides: Dict[str, str] = {}
        for owner_name, target_agent_key in owner_overrides_raw.items():
            normalized_owner_name = _normalize_owner_name(owner_name)
            normalized_target = str(target_agent_key or "").strip()
            if not normalized_owner_name or not normalized_target:
                raise ValueError(
                    f"Department '{department_id}' has an invalid owner_overrides entry"
                )
            if normalized_target == ADMIN_LOGIN_AGENT_KEY:
                raise ValueError(
                    f"agent_key '{ADMIN_LOGIN_AGENT_KEY}' is the shared admin login and cannot be an owner_overrides target"
                )
            if normalized_target not in agent_keys:
                raise ValueError(
                    f"Department '{department_id}' owner_overrides target '{normalized_target}' "
                    "must also be listed in agent_keys"
                )
            owner_overrides[normalized_owner_name] = normalized_target

        routes[department_id] = DepartmentRoute(
            department_id=department_id,
            label=str(raw.get("label") or "").strip(),
            mode=mode,
            agent_keys=agent_keys,
            active=active,
            owner_overrides=owner_overrides,
        )

    if not routes:
        log_event(logger, "auto_assign_department_routing_empty", source=source)
    return routes


def load_department_routing() -> Dict[str, DepartmentRoute]:
    source = os.getenv(AGENTS_SOURCE_ENV, "file").strip().lower()

    if source == "firestore":
        payload = _load_department_routing_payload_from_firestore()
    else:
        payload = _load_department_routing_payload_from_file()

    return _parse_department_routing_payload(payload, source=source)


def is_within_window(now: Optional[datetime] = None) -> bool:
    tz = ZoneInfo(TIMEZONE)
    current = now.astimezone(tz) if now else datetime.now(tz)
    if current.hour < WINDOW_START_HOUR:
        return False
    if current.hour == WINDOW_START_HOUR and current.minute < WINDOW_START_MINUTE:
        return False
    if current.hour > WINDOW_END_HOUR:
        return False
    if current.hour == WINDOW_END_HOUR and current.minute > WINDOW_END_MINUTE:
        return False
    return True


def _today_key(now: Optional[datetime] = None) -> str:
    tz = ZoneInfo(TIMEZONE)
    current = now.astimezone(tz) if now else datetime.now(tz)
    return current.strftime("%Y-%m-%d")


def _select_round_robin(
    agents: List[Agent],
    counts: Dict[str, int],
    last_index: int,
    exclude_agent_ids: Optional[set[str]] = None,
) -> Optional[Tuple[int, Agent]]:
    if not agents:
        return None
    exclude_agent_ids = exclude_agent_ids or set()
    total = len(agents)
    start = (last_index + 1) % total if last_index >= 0 else 0
    for offset in range(total):
        idx = (start + offset) % total
        agent = agents[idx]
        if agent.agent_id in exclude_agent_ids:
            continue
        if counts.get(agent.agent_id, 0) < MAX_ASSIGNMENTS_PER_AGENT:
            return idx, agent
    return None


def get_existing_assignment(conv_code: str) -> Optional[Dict[str, object]]:
    store = get_store()
    return store.get_assignment(conv_code)


def record_existing_assignment(
    *,
    conv_code: str,
    agent_id: str,
    reason: str,
    now: Optional[datetime] = None,
) -> Dict[str, object]:
    current = now.astimezone(ZoneInfo(TIMEZONE)) if now else datetime.now(ZoneInfo(TIMEZONE))
    store = get_store()
    return store.record_assignment(
        conv_code=conv_code,
        agent_id=agent_id,
        reason=reason,
        status="already_assigned",
        now=current,
    )


def plan_next_assignment(
    *,
    now: Optional[datetime] = None,
    exclude_agent_ids: Optional[set[str]] = None,
    department_id: Optional[str] = None,
    owner_name: Optional[str] = None,
) -> Dict[str, object]:
    current = now.astimezone(ZoneInfo(TIMEZONE)) if now else datetime.now(ZoneInfo(TIMEZONE))
    if not is_within_window(current):
        return {
            "status": "outside_hours",
            "agent_id": None,
            "reason": "outside_time_window",
        }

    exclude_agent_ids = exclude_agent_ids or set()
    active_agents = [agent for agent in load_agents() if agent.active]
    agents_by_id = {agent.agent_id: agent for agent in active_agents}

    store = get_store()
    day_key = _today_key(current)

    if department_id:
        try:
            routing = load_department_routing()
        except Exception as exc:
            log_event(
                logger,
                "auto_assign_department_routing_config_error",
                department_id=department_id,
                error=str(exc),
            )
            return {
                "status": "failed",
                "agent_id": None,
                "reason": "department_routing_config_error",
            }

        route = routing.get(department_id)
        if route is None or not route.active:
            log_event(logger, "auto_assign_unmapped_department", department_id=department_id)
            return {
                "status": "no_eligible_agents",
                "agent_id": None,
                "reason": "unmapped_department",
            }

        if route.mode == ROUTING_MODE_DIRECT:
            target_agent_id = route.agent_keys[0] if route.agent_keys else None
            target_agent = agents_by_id.get(target_agent_id) if target_agent_id else None
            if target_agent is None or target_agent.agent_id in exclude_agent_ids:
                log_event(
                    logger,
                    "auto_assign_direct_no_alternative_agent",
                    department_id=department_id,
                    agent_id=target_agent_id,
                )
                return {
                    "status": "no_eligible_agents",
                    "agent_id": None,
                    "reason": "direct_no_alternative_agent",
                }
            return {
                "status": "candidate",
                "agent_id": target_agent.agent_id,
                "reason": "direct_department",
                "day_key": day_key,
                "next_index": None,
                "pool_key": f"direct:{department_id}",
                "bypass_quota": True,
            }

        if route.mode == ROUTING_MODE_TEAM_POOL:
            if route.owner_overrides and owner_name:
                override_agent_id = route.owner_overrides.get(_normalize_owner_name(owner_name))
                if override_agent_id:
                    override_agent = agents_by_id.get(override_agent_id)
                    if override_agent and override_agent.agent_id not in exclude_agent_ids:
                        log_event(
                            logger,
                            "auto_assign_team_pool_owner_override",
                            department_id=department_id,
                            agent_id=override_agent.agent_id,
                        )
                        return {
                            "status": "candidate",
                            "agent_id": override_agent.agent_id,
                            "reason": "team_pool_owner_override",
                            "day_key": day_key,
                            "next_index": None,
                            "pool_key": f"owner_override:{department_id}",
                            "bypass_quota": True,
                        }
                    # Matched agent is inactive/excluded - the team has other members,
                    # so fall through to normal round-robin instead of failing safe.

            pool_key = f"team:{department_id}"
            pool_agents = [agents_by_id[key] for key in route.agent_keys if key in agents_by_id]
            day_state = store.get_daily_state(day_key, pool_key=pool_key)
            counts = dict(day_state.get("counts") or {})
            last_index = int(day_state.get("last_index", -1))
            pick = _select_round_robin(pool_agents, counts, last_index, exclude_agent_ids=exclude_agent_ids)
            if pick is None:
                return {
                    "status": "no_eligible_agents",
                    "agent_id": None,
                    "reason": "quota_reached",
                }
            next_index, chosen = pick
            return {
                "status": "candidate",
                "agent_id": chosen.agent_id,
                "reason": "team_pool",
                "day_key": day_key,
                "next_index": next_index,
                "pool_key": pool_key,
                "bypass_quota": False,
            }

        # route.mode == ROUTING_MODE_FULL_POOL falls through to the shared pool below.

    if not active_agents:
        return {
            "status": "no_eligible_agents",
            "agent_id": None,
            "reason": "no_active_agents",
        }

    day_state = store.get_daily_state(day_key, pool_key=FULL_POOL_KEY)
    counts = dict(day_state.get("counts") or {})
    last_index = int(day_state.get("last_index", -1))

    pick = _select_round_robin(active_agents, counts, last_index, exclude_agent_ids=exclude_agent_ids)
    if pick is None:
        return {
            "status": "no_eligible_agents",
            "agent_id": None,
            "reason": "quota_reached",
        }

    next_index, chosen = pick
    return {
        "status": "candidate",
        "agent_id": chosen.agent_id,
        "reason": "round_robin",
        "day_key": day_key,
        "next_index": next_index,
        "pool_key": FULL_POOL_KEY,
        "bypass_quota": False,
    }

def commit_assignment(
    *,
    conv_code: str,
    agent_id: str,
    reason: str,
    now: Optional[datetime] = None,
    day_key: Optional[str] = None,
    next_index: Optional[int] = None,
    pool_key: str = FULL_POOL_KEY,
    bypass_quota: bool = False,
) -> Dict[str, object]:
    current = now.astimezone(ZoneInfo(TIMEZONE)) if now else datetime.now(ZoneInfo(TIMEZONE))
    store = get_store()
    if day_key is None:
        day_key = _today_key(current)
    return store.commit_assignment(
        conv_code=conv_code,
        agent_id=agent_id,
        reason=reason,
        date_key=day_key,
        next_index=next_index,
        max_assignments=None if bypass_quota else MAX_ASSIGNMENTS_PER_AGENT,
        pool_key=pool_key,
        now=current,
    )


def assign_round_robin(
    *,
    conv_code: str,
    incoming_agent_id: Optional[str],
    now: Optional[datetime] = None,
    department_id: Optional[str] = None,
) -> Dict[str, object]:
    current = now.astimezone(ZoneInfo(TIMEZONE)) if now else datetime.now(ZoneInfo(TIMEZONE))
    existing = get_existing_assignment(conv_code)
    if existing:
        log_event(
            logger,
            "auto_assign_replay",
            conv_code=conv_code,
            agent_id=existing.get("agent_id"),
        )
        return {
            "status": "already_assigned",
            "conv_code": conv_code,
            "agent_id": existing.get("agent_id"),
            "reason": "idempotent_replay",
        }

    if incoming_agent_id:
        record = record_existing_assignment(
            conv_code=conv_code,
            agent_id=incoming_agent_id,
            reason="incoming_agent_id",
            now=current,
        )
        log_event(
            logger,
            "auto_assign_incoming_agent",
            conv_code=conv_code,
            agent_id=incoming_agent_id,
        )
        return {
            "status": "already_assigned",
            "conv_code": conv_code,
            "agent_id": record.get("agent_id"),
            "reason": "incoming_agent_id",
        }

    plan = plan_next_assignment(now=current, department_id=department_id)
    if plan.get("status") != "candidate":
        status = plan.get("status")
        reason = plan.get("reason")
        if status == "outside_hours":
            log_event(logger, "auto_assign_outside_hours", conv_code=conv_code)
        if status == "no_eligible_agents" and reason == "quota_reached":
            log_event(logger, "auto_assign_quota_reached", conv_code=conv_code)
        return {
            "status": status,
            "conv_code": conv_code,
            "agent_id": plan.get("agent_id"),
            "reason": reason,
        }

    commit = commit_assignment(
        conv_code=conv_code,
        agent_id=str(plan.get("agent_id")),
        reason=str(plan.get("reason") or "round_robin"),
        now=current,
        day_key=str(plan.get("day_key")),
        next_index=plan.get("next_index"),
        pool_key=str(plan.get("pool_key") or FULL_POOL_KEY),
        bypass_quota=bool(plan.get("bypass_quota", False)),
    )
    if commit.get("status") == "assigned":
        log_event(
            logger,
            "auto_assign_success",
            conv_code=conv_code,
            agent_id=commit.get("agent_id"),
        )
    return commit
