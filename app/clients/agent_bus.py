from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx

RUNTIME_VALIDATION_TOKEN_HEADER = "X-Runtime-Validation-Token"
RUNTIME_VALIDATION_TOKEN_ENV = "AGENT_BUS_RUNTIME_VALIDATION_TOKEN"


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
        runtime_validation_token: str | None = None,
        timeout_seconds: int = 30,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else ""
        self._token = token
        self._runtime_validation_token = runtime_validation_token or os.getenv(RUNTIME_VALIDATION_TOKEN_ENV)
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

    async def create_review_packet(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._base_url:
            raise MissingAgentBusBaseUrlError("AGENT_BUS_BASE_URL is required for Agent Bus dispatch.")
        response = await self._client.post(
            f"{self._base_url}/review-packets",
            headers=self._headers(),
            json=payload,
        )
        return _object_response(response, "POST", "/review-packets")

    async def attach_review_to_work_item(self, work_item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._base_url:
            raise MissingAgentBusBaseUrlError("AGENT_BUS_BASE_URL is required for Agent Bus dispatch.")
        path = f"/work-items/{quote(work_item_id, safe='')}/review"
        response = await self._client.post(
            f"{self._base_url}{path}",
            headers=self._headers(),
            json=payload,
        )
        return _object_response(response, "POST", path)

    async def transition_work_item(
        self,
        work_item_id: str,
        *,
        status: str,
        actor: str,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        owner_agent: str | None = None,
        review_agent: str | None = None,
    ) -> dict[str, Any]:
        if not self._base_url:
            raise MissingAgentBusBaseUrlError("AGENT_BUS_BASE_URL is required for Agent Bus dispatch.")
        path = f"/work-items/{quote(work_item_id, safe='')}/transition"
        payload = _compact_payload(
            {
                "status": status,
                "actor": actor,
                "reason": reason,
                "metadata": metadata,
                "owner_agent": owner_agent,
                "review_agent": review_agent,
            }
        )
        response = await self._client.post(
            f"{self._base_url}{path}",
            headers=self._headers(),
            json=payload,
        )
        return _object_response(response, "POST", path)

    async def complete_work_item(
        self,
        work_item_id: str,
        *,
        actor: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._base_url:
            raise MissingAgentBusBaseUrlError("AGENT_BUS_BASE_URL is required for Agent Bus dispatch.")
        path = f"/work-items/{quote(work_item_id, safe='')}/complete"
        response = await self._client.post(
            f"{self._base_url}{path}",
            headers=self._headers(),
            json=_compact_payload({"actor": actor, "metadata": metadata}),
        )
        return _object_response(response, "POST", path)

    async def get_agent_status(self, agent_id: str) -> dict[str, Any]:
        if not self._base_url:
            raise MissingAgentBusBaseUrlError("AGENT_BUS_BASE_URL is required for Agent Bus dispatch.")
        path = f"/agents/{quote(agent_id, safe='')}/status"
        response = await self._client.get(
            f"{self._base_url}{path}",
            headers=self._headers(),
        )
        return _object_response(response, "GET", path)

    async def get_agent_queue(self, agent_id: str) -> list[dict[str, Any]]:
        if not self._base_url:
            raise MissingAgentBusBaseUrlError("AGENT_BUS_BASE_URL is required for Agent Bus dispatch.")
        path = f"/agents/{quote(agent_id, safe='')}/queue"
        response = await self._client.get(
            f"{self._base_url}{path}",
            headers=self._headers(),
        )
        return _list_response(response, "GET", path)

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=float(self._timeout_seconds))
        return self._http_client

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._runtime_validation_token:
            headers[RUNTIME_VALIDATION_TOKEN_HEADER] = self._runtime_validation_token
        return headers

    def _runtime_validation_headers(self) -> dict[str, str]:
        return self._headers()


def _compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


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


def _list_response(response: httpx.Response, method: str, path: str) -> list[dict[str, Any]]:
    if response.status_code < 200 or response.status_code >= 300:
        raise AgentBusAPIError(method, path, response.status_code, _response_detail(response))
    try:
        data = response.json()
    except ValueError as exc:
        raise AgentBusAPIError(method, path, response.status_code, "Malformed JSON response.") from exc
    if not isinstance(data, list):
        raise AgentBusAPIError(method, path, response.status_code, "Expected list response.")
    if not all(isinstance(item, dict) for item in data):
        raise AgentBusAPIError(method, path, response.status_code, "Expected list of object responses.")
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
