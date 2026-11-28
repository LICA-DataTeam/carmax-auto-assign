"""Traffic-triggered auto-reassign sweep.

Each /auto-assign webhook schedules `maybe_run_sweep` as a background task
(after the response is sent). It runs a sweep only when:
  * reassign is enabled (see reassign_settings), and
  * this instance's local gate allows it (avoids a Firestore round-trip on
    every webhook), and
  * the shared Firestore lease is acquired: at most one sweep START per
    SWEEP_INTERVAL_SECONDS across all Cloud Run instances, and never two
    sweeps holding the lease at once (TTL bounds a stuck sweep).
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from datetime import datetime, timedelta
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from src.api.services import reassign_settings
from src.api.services.auto_assign_store import get_store
from src.api.services.bq_logger import log_bq_event
from src.api.utils.logging import get_logger, log_event

logger = get_logger(__name__)

TIMEZONE = "Asia/Manila"
SWEEP_INTERVAL_SECONDS = 60
# Upper bound on how long one sweep can hold the lease. On request-based CPU
# a background sweep can be throttled after the webhook response, so this must
# comfortably exceed a normal sweep but still let a stuck one expire.
LEASE_TTL_SECONDS = 300

_INSTANCE_ID = f"{os.getenv('K_REVISION', 'local')}:{uuid.uuid4().hex[:12]}"

_LOCAL_LOCK = threading.Lock()
_LOCAL_RUNNING = False
_LOCAL_NEXT_CHECK: Optional[datetime] = None


def _now() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


def reset_local_state() -> None:
    """Forget this instance's gate (tests simulate a second instance this way)."""
    global _LOCAL_RUNNING, _LOCAL_NEXT_CHECK
    with _LOCAL_LOCK:
        _LOCAL_RUNNING = False
        _LOCAL_NEXT_CHECK = None


async def run_sweep_with_lease(
    *,
    trigger: str,
    limit: Optional[int] = None,
    settings: Optional[reassign_settings.ReassignSettings] = None,
) -> Dict[str, object]:
    """Acquire the shared lease and run one sweep. Used by the traffic trigger
    and by POST /auto-reassign, so the two can never sweep at the same time."""
    global _LOCAL_NEXT_CHECK
    from src.api.services.auto_reassign import run_auto_reassign

    settings = settings or reassign_settings.load_settings()
    if not settings.enabled:
        return {"status": "disabled", "checked": 0, "reassigned": 0, "results": []}

    store = get_store()
    now = _now()
    holder = f"{_INSTANCE_ID}:{uuid.uuid4().hex[:8]}"
    lease = store.try_acquire_reassign_lease(
        holder=holder,
        now=now,
        min_interval_seconds=SWEEP_INTERVAL_SECONDS,
        ttl_seconds=LEASE_TTL_SECONDS,
    )
    next_allowed = lease.get("next_allowed_at")
    with _LOCAL_LOCK:
        if isinstance(next_allowed, datetime):
            _LOCAL_NEXT_CHECK = next_allowed
    if not lease.get("acquired"):
        log_event(logger, "auto_reassign_sweep_throttled", trigger=trigger, holder=holder)
        return {"status": "throttled", "checked": 0, "reassigned": 0, "results": []}

    request_id = uuid.uuid4().hex
    log_event(logger, "auto_reassign_sweep_start", trigger=trigger, holder=holder, request_id=request_id)
    try:
        result = await run_auto_reassign(limit=limit, request_id=request_id, settings=settings)
    finally:
        try:
            store.release_reassign_lease(holder=holder, now=_now())
        except Exception as exc:  # lease TTL still bounds it
            log_event(logger, "auto_reassign_lease_release_failed", holder=holder, error=str(exc))
    log_event(
        logger,
        "auto_reassign_complete",
        trigger=trigger,
        checked=result.get("checked"),
        reassigned=result.get("reassigned"),
    )
    log_bq_event(
        "auto_reassign_complete",
        request_id=request_id,
        checked=result.get("checked"),
        reassigned=result.get("reassigned"),
    )
    return result


def maybe_run_sweep() -> Dict[str, object]:
    """Sync entry point for FastAPI BackgroundTasks: Starlette runs sync tasks
    in a worker thread, and the sweep gets its own event loop there, so its
    blocking Firestore calls never stall the server's loop (other webhooks)."""
    global _LOCAL_RUNNING, _LOCAL_NEXT_CHECK
    try:
        settings = reassign_settings.load_settings()
    except Exception as exc:
        log_event(logger, "auto_reassign_sweep_settings_failed", error=str(exc))
        return {"status": "error"}
    if not settings.enabled:
        return {"status": "disabled"}

    now = _now()
    with _LOCAL_LOCK:
        if _LOCAL_RUNNING:
            return {"status": "busy"}
        if _LOCAL_NEXT_CHECK is not None and now < _LOCAL_NEXT_CHECK:
            return {"status": "throttled_local"}
        _LOCAL_RUNNING = True
    try:
        return asyncio.run(run_sweep_with_lease(trigger="traffic", settings=settings))
    except Exception as exc:
        log_event(logger, "auto_reassign_sweep_failed", error=str(exc))
        return {"status": "error"}
    finally:
        with _LOCAL_LOCK:
            _LOCAL_RUNNING = False
            if _LOCAL_NEXT_CHECK is None:
                _LOCAL_NEXT_CHECK = now + timedelta(seconds=SWEEP_INTERVAL_SECONDS)
