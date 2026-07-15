from __future__ import annotations

import re
from typing import Iterator, Optional

# LiveAgent injects a system message with this literal text on private
# Facebook messages sent to a specific connected page, e.g.:
#   "Private message to: <a href='...'>Carmax Authorized Agent - Mark john Castro</a>"
# This is the only place the page name appears - the ticket's own owner_name
# field carries the customer's name instead for these messages.
_PRIVATE_MESSAGE_PAGE_PATTERN = re.compile(
    r"Private message to:\s*<a[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL
)


def _iter_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def extract_private_message_page_name(messages_payload: object) -> Optional[str]:
    """Scan a raw GET /tickets/{id}/messages payload for the
    "Private message to: <a>PAGE NAME</a>" system message and return the
    page name, or None if not present. Walks the payload defensively
    (dicts/lists of unknown shape) since the exact nesting of LiveAgent's
    messages response isn't guaranteed."""
    for text in _iter_strings(messages_payload):
        match = _PRIVATE_MESSAGE_PAGE_PATTERN.search(text)
        if match:
            page_name = match.group(1).strip()
            if page_name:
                return page_name
    return None
