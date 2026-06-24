from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class AgentBusClientError(Exception):
    """Base error for Agent Bus client failures."""


class MissingAgentBusBaseUrlError(AgentBusClientError):
    """Raised when Agent Bus dispatch is enabled without a base URL."""


class AgentBusAPIError(AgentBusClientError):
    def __init__(self, method: str, path: str, status_code: int, detail: str) -> None:
        super().__init__(f"Agent Bus {method} {path} failed with {status_code}: {detail}")
        self.method = method
        self.path = path
        self.status_code = status_code
        self.detail = detail


class AgentBusClient:
    """Small Agent Bus API wrapper for documented WorkItem operations."""

    def __init__(
        self,
        *,
        base_url: str | None,
        token: str | None = None,
        timeout_seconds: int = 30,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else ""
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()

    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._base_url:
            raise MissingAgentBusBaseUrlError("AGENT_BUS_BASE_URL is required for Agent Bus dispatch.")
        response = await self._client.post(
            f"{self._base_url}/work-items",
            headers=self._headers(),
            json=payload,
        )
        return _object_response(response, "POST", "/work-items")

    async def get_work_item(self, work_item_id: str) -> dict[str, Any]:
        if not self._base_url:
            raise MissingAgentBusBaseUrlError("AGENT_BUS_BASE_URL is required for Agent Bus dispatch.")
        path = f"/work-items/{quote(work_item_id, safe='')}"
        response = await self._client.get(
            f"{self._base_url}{path}",
            headers=self._headers(),
        )
        return _object_response(response, "GET", path)

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=float(self._timeout_seconds))
        return self._http_client

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers


def _object_response(response: httpx.Response, method: str, path: str) -> dict[str, Any]:
    if response.status_code < 200 or response.status_code >= 300:
        raise AgentBusAPIError(method, path, response.status_code, _response_detail(response))
    try:
        data = response.json()
    except ValueError as exc:
        raise AgentBusAPIError(method, path, response.status_code, "Malformed JSON response.") from exc
    if not isinstance(data, dict):
        raise AgentBusAPIError(method, path, response.status_code, "Expected object response.")
    return data


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message") or payload
        return str(detail)
    return str(payload)
