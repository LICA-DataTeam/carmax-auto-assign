from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_BASE_URL = "https://carmaxphilippines.ladesk.com/api/v3"


@dataclass(frozen=True)
class LiveAgentSettings:
    base_url: str
    api_key: str
    api_header: str
    timeout_seconds: float
    verify_ssl: bool


def _bool_env(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y"}


def load_liveagent_settings() -> LiveAgentSettings:
    base_url = os.getenv("LIVEAGENT_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    api_key = os.getenv("LIVEAGENT_API_KEY", "").strip()
    api_header = os.getenv("LIVEAGENT_API_HEADER", "apikey").strip()
    timeout_seconds = float(os.getenv("LIVEAGENT_TIMEOUT_SECONDS", "10").strip())
    verify_ssl = _bool_env(os.getenv("LIVEAGENT_VERIFY_SSL"), True)

    return LiveAgentSettings(
        base_url=base_url,
        api_key=api_key,
        api_header=api_header,
        timeout_seconds=timeout_seconds,
        verify_ssl=verify_ssl,
    )
