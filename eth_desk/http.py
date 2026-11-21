from __future__ import annotations

import httpx

HEADERS = {
    "User-Agent": "eth-desk/0.1",
    "Accept": "application/json",
}


class HttpxJson:
    """GET JSON. `base_url` is prefixed onto paths that start with `/`."""

    def __init__(self, base_url: str = ""):
        self.base_url = base_url.rstrip("/")

    def __call__(self, path_or_url: str, params: dict | None = None):
        url = path_or_url
        if self.base_url and path_or_url.startswith("/"):
            url = self.base_url + path_or_url
        response = httpx.get(
            url,
            params=params,
            headers=HEADERS,
            timeout=10.0,
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.json()
