from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_STATUS_FORCELIST = (429, 500, 502, 503, 504)
DEFAULT_ALLOWED_METHODS = frozenset({"HEAD", "GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


def build_retry_session(
    total_retries: int = 4,
    backoff_factor: float = 0.5,
    status_forcelist: tuple[int, ...] = DEFAULT_STATUS_FORCELIST,
    allowed_methods=frozenset(DEFAULT_ALLOWED_METHODS),
    user_agent: str = "NotionLocalSync/2.0",
) -> requests.Session:
    retry = Retry(
        total=max(0, int(total_retries or 0)),
        read=max(0, int(total_retries or 0)),
        connect=max(0, int(total_retries or 0)),
        backoff_factor=float(backoff_factor or 0),
        status_forcelist=tuple(status_forcelist or DEFAULT_STATUS_FORCELIST),
        allowed_methods=allowed_methods or DEFAULT_ALLOWED_METHODS,
        raise_on_status=False,
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": str(user_agent or "NotionLocalSync/2.0"),
        }
    )
    return session
