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
from src.integrations.liveagent import LiveAgentClient


logger = get_logger(__name__)

TIMEZONE = "Asia/Manila"
REASSIGN_AFTER_MINUTES = int(os.getenv("REASSIGN_AFTER_MINUTES", "10"))
REASSIGN_MAX_ATTEMPTS = int(os.getenv("REASSIGN_MAX_ATTEMPTS", "1"))
REASSIGN_ELIGIBLE_STATUSES = os.getenv("REASSIGN_ELIGIBLE_STATUSES", "C,A")
REASSIGN_STATUS = os.getenv("LIVEAGENT_REASSIGN_STATUS", os.getenv("LIVEAGENT_ASSIGN_STATUS", "N"))


@dataclass
class ReassignResult:
    conv_code: str
    status: str
    agent_id: Optional[str] = None
    reason: Optional[str] = None


def _eligible_statuses() -> List[str]:
    return [item.strip() for item in REASSIGN_ELIGIBLE_STATUSES.split(",") if item.strip()]


def _now() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


async def run_auto_reassign(
    *,
    limit: Optional[int] = None,
    request_id: Optional[str] = None,
) -> Dict[str, object]:
    request_id = request_id or uuid.uuid4().hex
    now = _now()
    cutoff = now - timedelta(minutes=REASSIGN_AFTER_MINUTES)
    statuses = _eligible_statuses()
    store = get_store()

    candidates = store.list_stale_assignments(
        cutoff=cutoff,
        max_reassign_count=REASSIGN_MAX_ATTEMPTS,
        statuses=statuses,
    )
    if limit is not None:
        candidates = candidates[: max(0, int(limit))]

    results: List[Dict[str, object]] = []
    log_bq_event(
        "auto_reassign_request",
        request_id=request_id,
        limit=limit,
        candidate_count=len(candidates),
        cutoff_minutes=REASSIGN_AFTER_MINUTES,
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
            plan = auto_assign.plan_next_assignment(
                now=now,
                exclude_agent_ids={str(current_agent)} if current_agent else None,
                department_id=department_id,
                owner_name=owner_name,
            )
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
                status=REASSIGN_STATUS or None,
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

            bypass_quota = bool(plan.get("bypass_quota", False))
            commit = store.commit_reassign(
                conv_code=conv_code,
                new_agent_id=str(plan.get("agent_id")),
                previous_agent_id=str(current_agent) if current_agent else None,
                reason="reassign_timeout",
                date_key=str(plan.get("day_key")),
                next_index=plan.get("next_index"),
                max_reassign_count=REASSIGN_MAX_ATTEMPTS,
                max_assignments=None if bypass_quota else auto_assign.MAX_ASSIGNMENTS_PER_AGENT,
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
