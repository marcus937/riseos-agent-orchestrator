from __future__ import annotations

from typing import Any

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
    """Small Agent Bus API wrapper for work-item creation."""

    def __init__(
        self,
        *,
        base_url: str | None,
        token: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else ""
        self._token = token
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
        if response.status_code < 200 or response.status_code >= 300:
            raise AgentBusAPIError("POST", "/work-items", response.status_code, _response_detail(response))
        data = response.json()
        if not isinstance(data, dict):
            raise AgentBusAPIError("POST", "/work-items", response.status_code, "Expected object response.")
        return data

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=20.0)
        return self._http_client

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message") or payload
        return str(detail)
    return str(payload)
