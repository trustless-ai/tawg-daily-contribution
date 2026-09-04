"""Token-safe HTTP helpers for external source adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx


class SafeHttpError(RuntimeError):
    """An HTTP failure whose message intentionally omits request URLs."""


class SafeJsonHttpClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def post_form(self, url: str, data: Mapping[str, object]) -> dict[str, Any]:
        try:
            response = await self.client.post(url, data=data)
        except httpx.HTTPError:
            raise SafeHttpError("external HTTP request failed") from None
        if not response.is_success:
            raise SafeHttpError(f"external HTTP request returned status {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise SafeHttpError("external HTTP response was not JSON") from None
        if not isinstance(payload, dict):
            raise SafeHttpError("external HTTP response was not an object")
        return payload

    async def post_json(
        self,
        url: str,
        body: Mapping[str, object],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Same contract as post_form, JSON body instead of form-encoded -- added for the
        invinoveritas verification client, which expects `Content-Type: application/json`."""
        try:
            response = await self.client.post(url, json=dict(body), headers=headers)
        except httpx.HTTPError:
            raise SafeHttpError("external HTTP request failed") from None
        if not response.is_success:
            raise SafeHttpError(f"external HTTP request returned status {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise SafeHttpError("external HTTP response was not JSON") from None
        if not isinstance(payload, dict):
            raise SafeHttpError("external HTTP response was not an object")
        return payload
