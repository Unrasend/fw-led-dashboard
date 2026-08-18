from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

MAX_JSON_BYTES = 32 * 1024


class JsonHttpClient:
    def __init__(
        self,
        *,
        allowed_hosts: Iterable[str],
        opener: Callable[..., Any] = urlopen,
        timeout_seconds: float = 4.0,
    ) -> None:
        self._allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self._opener = opener
        self._timeout = timeout_seconds

    def get(self, url: str) -> object:
        self._require_allowed_url(url)
        request = Request(url, headers={"User-Agent": "framework-led-dashboard/0.1"})
        with self._opener(request, timeout=self._timeout) as response:
            self._require_allowed_url(response.geturl())
            body = response.read(MAX_JSON_BYTES + 1)
        if len(body) > MAX_JSON_BYTES:
            raise ValueError("JSON response is too large")
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("response is not valid JSON") from error

    def _require_allowed_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.lower() not in self._allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("URL must use an allowed HTTPS host")
