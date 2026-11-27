from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from src.api.models import AssignTicketRequest
from src.api.services import auto_assign
from src.api.services.auto_assign_store import get_store
from src.api.services.bq_logger import log_bq_event
from src.api.utils.logging import get_logger, log_event
from src.integrations.liveagent import LiveAgentClient, extract_private_message_page_name


logger = get_logger(__name__)

TIMEZONE = "Asia/Manila"

# Read per run (not at import) so a Cloud Run env change or a test override is
# always the value in effect.
#   REASSIGN_AFTER_MINUTES      minutes without action before a ticket is reassigned
#   REASSIGN_MAX_ATTEMPTS       max times one ticket may be reassigned (0 = disabled)
#   REASSIGN_ELIGIBLE_STATUSES  LiveAgent ticket statuses that count as "unanswered"
#                               (N = New - what auto-assign sets on assignment)
#   REASSIGN_MAX_AGE_MINUTES    ignore tickets whose last action is older than this,
#                               so enabling reassign never sweeps up old history
#   REASSIGN_MAX_PER_RUN        cap on tickets processed per /auto-reassign call
DEFAULT_AFTER_MINUTES = 10
DEFAULT_MAX_ATTEMPTS = 1
DEFAULT_ELIGIBLE_STATUSES = "C,A"
DEFAULT_MAX_AGE_MINUTES = 120
DEFAULT_MAX_PER_RUN = 50


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        log_event(logger, "auto_reassign_invalid_env", name=name, value=raw)
        return default


def _after_minutes() -> int:
    return _env_int("REASSIGN_AFTER_MINUTES", DEFAULT_AFTER_MINUTES, minimum=1)


def _max_attempts() -> int:
    return _env_int("REASSIGN_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)


def _max_age_minutes() -> int:
    return _env_int("REASSIGN_MAX_AGE_MINUTES", DEFAULT_MAX_AGE_MINUTES, minimum=1)


def _max_per_run() -> int:
    return _env_int("REASSIGN_MAX_PER_RUN", DEFAULT_MAX_PER_RUN, minimum=1)


def _reassign_status() -> str:
    return os.getenv("LIVEAGENT_REASSIGN_STATUS", os.getenv("LIVEAGENT_ASSIGN_STATUS", "N"))


@dataclass
class ReassignResult:
    conv_code: str
    status: str
    agent_id: Optional[str] = None
    reason: Optional[str] = None


def _eligible_statuses() -> List[str]:
    raw = os.getenv("REASSIGN_ELIGIBLE_STATUSES", DEFAULT_ELIGIBLE_STATUSES)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _now() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


async def _resolve_owner_override_fallback_name(
    client: LiveAgentClient, conv_code: str
) -> Optional[str]:
    """See src.api.routes.assign._resolve_owner_override_fallback_name - same
    fallback, used here so stale team_pool tickets re-checked by auto-reassign
    get the same owner_overrides treatment as the initial assign."""
    try:
        messages_payload = await client.get_ticket_messages(conv_code)
    except Exception as exc:
        log_event(
            logger,
            "auto_reassign_owner_override_fallback_fetch_failed",
            conv_code=conv_code,
            error=str(exc),
        )
        return None
    return extract_private_message_page_name(messages_payload)


async def _fetch_offline_agent_ids(client: LiveAgentClient, request_id: str) -> set[str]:
    """See src.api.routes.assign._fetch_offline_agent_ids - same fail-open
    fetch, used here so stale-assignment reassignment also skips an employed
    agent (status "A") who is currently offline in LiveAgent (online_status
    "F")."""
    try:
        agents = await client.list_agents()
    except Exception as exc:
        log_event(
            logger,
            "auto_reassign_online_status_fetch_failed",
            request_id=request_id,
            error=str(exc),
        )
        return set()
    return {
        str(agent.id)
        for agent in agents
        if agent.id and agent.status == "A" and agent.online_status == "F"
    }


async def run_auto_reassign(
    *,
    limit: Optional[int] = None,
    request_id: Optional[str] = None,
) -> Dict[str, object]:
    request_id = request_id or uuid.uuid4().hex
    now = _now()
    after_minutes = _after_minutes()
    max_attempts = _max_attempts()
    cutoff = now - timedelta(minutes=after_minutes)
    not_before = now - timedelta(minutes=max(_max_age_minutes(), after_minutes))
    statuses = _eligible_statuses()
    store = get_store()

    if max_attempts <= 0:
        candidates: List[Dict[str, object]] = []
    else:
        candidates = store.list_stale_assignments(
            cutoff=cutoff,
            max_reassign_count=max_attempts,
            statuses=statuses,
            not_before=not_before,
        )
    run_cap = _max_per_run() if limit is None else max(0, int(limit))
    candidates = candidates[:run_cap]

    results: List[Dict[str, object]] = []
    log_bq_event(
        "auto_reassign_request",
        request_id=request_id,
        limit=limit,
        candidate_count=len(candidates),
        cutoff_minutes=after_minutes,
    )
    if not candidates:
        return {
            "status": "ok",
            "checked": 0,
            "reassigned": 0,
            "results": results,
        }

    client = LiveAgentClient()
    try:
        offline_agent_ids = await _fetch_offline_agent_ids(client, request_id)
        for record in candidates:
            conv_code = str(record.get("conv_code") or "")
            if not conv_code:
                continue

            try:
                ticket = await client.get_ticket(conv_code)
            except Exception as exc:
                log_event(logger, "auto_reassign_ticket_fetch_failed", conv_code=conv_code, error=str(exc))
                log_bq_event(
                    "auto_reassign_failed",
                    request_id=request_id,
                    conv_code=conv_code,
                    reason="ticket_fetch_failed",
                )
                results.append(
                    {"conv_code": conv_code, "status": "failed", "reason": "ticket_fetch_failed"}
                )
                continue

            ticket_status = getattr(ticket, "status", None)
            if ticket_status not in statuses:
                # Answered / resolved etc. Record it so later scans skip this
                # ticket without another LiveAgent call.
                try:
                    store.mark_ticket_status(conv_code, ticket_status)
                except Exception as exc:
                    log_event(
                        logger,
                        "auto_reassign_mark_status_failed",
                        conv_code=conv_code,
                        error=str(exc),
                    )
                log_event(
                    logger,
                    "auto_reassign_skip_status",
                    conv_code=conv_code,
                    status=ticket_status,
                )
                log_bq_event(
                    "auto_reassign_decision",
                    request_id=request_id,
                    conv_code=conv_code,
                    status="skipped",
                    reason="status_not_eligible",
                    ticket_status=ticket_status,
                )
                results.append(
                    {
                        "conv_code": conv_code,
                        "status": "skipped",
                        "reason": "status_not_eligible",
                    }
                )
                continue

            current_agent = getattr(ticket, "agent_id", None) or record.get("agent_id")
            department_id = getattr(ticket, "department_id", None)
            owner_name = getattr(ticket, "owner_name", None)
            exclude_agent_ids = set(offline_agent_ids)
            if current_agent:
                exclude_agent_ids.add(str(current_agent))
            plan = auto_assign.plan_next_assignment(
                now=now,
                exclude_agent_ids=exclude_agent_ids,
                department_id=department_id,
                owner_name=owner_name,
            )
            if (
                plan.get("status") == "candidate"
                and plan.get("reason") == "team_pool"
                and plan.get("owner_overrides_configured")
            ):
                fallback_owner_name = await _resolve_owner_override_fallback_name(client, conv_code)
                if fallback_owner_name:
                    fallback_plan = auto_assign.plan_next_assignment(
                        now=now,
                        exclude_agent_ids=exclude_agent_ids,
                        department_id=department_id,
                        owner_name=fallback_owner_name,
                        owner_name_source="ticket_message_fallback",
                    )
                    if fallback_plan.get("reason") == "team_pool_owner_override_message_fallback":
                        plan = fallback_plan
            if plan.get("status") != "candidate":
                log_bq_event(
                    "auto_reassign_decision",
                    request_id=request_id,
                    conv_code=conv_code,
                    status=plan.get("status"),
                    reason=plan.get("reason"),
                    department_id=department_id,
                )
                results.append(
                    {
                        "conv_code": conv_code,
                        "status": plan.get("status"),
                        "reason": plan.get("reason"),
                    }
                )
                continue

            payload = AssignTicketRequest(
                agent_id=str(plan.get("agent_id")),
                status=_reassign_status() or None,
            )
            log_bq_event(
                "auto_reassign_decision",
                request_id=request_id,
                conv_code=conv_code,
                status="candidate",
                agent_id=plan.get("agent_id"),
                reason=plan.get("reason"),
                department_id=department_id,
            )

            try:
                await client.assign_ticket(conv_code, payload)
            except Exception as exc:
                log_event(
                    logger,
                    "auto_reassign_liveagent_failed",
                    conv_code=conv_code,
                    error=str(exc),
                )
                log_bq_event(
                    "auto_reassign_failed",
                    request_id=request_id,
                    conv_code=conv_code,
                    reason="liveagent_error",
                )
                results.append(
                    {"conv_code": conv_code, "status": "failed", "reason": "liveagent_error"}
                )
                continue

            commit = store.commit_reassign(
                conv_code=conv_code,
                new_agent_id=str(plan.get("agent_id")),
                previous_agent_id=str(current_agent) if current_agent else None,
                reason="reassign_timeout",
                date_key=str(plan.get("day_key")),
                next_index=plan.get("next_index"),
                max_reassign_count=max_attempts,
                max_assignments=auto_assign.effective_max_assignments(
                    bypass_quota=bool(plan.get("bypass_quota", False)),
                    max_assignments=plan.get("max_assignments"),
                ),
                pool_key=str(plan.get("pool_key") or "full_pool"),
                now=now,
            )
            log_bq_event(
                "auto_reassign_success",
                request_id=request_id,
                conv_code=conv_code,
                agent_id=commit.get("agent_id"),
                reason=commit.get("reason"),
                previous_agent_id=current_agent,
                department_id=department_id,
            )
            results.append(commit)
    finally:
        await client.close()

    reassigned = len([r for r in results if r.get("status") == "reassigned"])
    return {
        "status": "ok",
        "checked": len(candidates),
        "reassigned": reassigned,
        "results": results,
    }
