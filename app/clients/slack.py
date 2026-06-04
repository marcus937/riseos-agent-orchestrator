from typing import Any

import httpx

from app.review_workflow import RequeueContext

SlackResponse = dict[str, Any]


class SlackClientError(Exception):
    """Base error for Slack client failures."""


class MissingSlackConfigError(SlackClientError):
    """Raised when Slack posting is requested without token or channel config."""


class SlackAPIError(SlackClientError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"Slack chat.postMessage failed with {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class SlackClient:
    def __init__(
        self,
        *,
        token: str | None = None,
        channel: str | None = None,
        api_base_url: str = "https://slack.com/api",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._channel = channel
        self._api_base_url = api_base_url.rstrip("/")
        self._http_client = http_client
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()

    async def post_requeue_task(self, context: RequeueContext) -> SlackResponse:
        if not self._token or not self._channel:
            raise MissingSlackConfigError("SLACK_BOT_TOKEN and SLACK_TASK_CHANNEL are required to post Slack tasks.")

        response = await self._client.post(
            f"{self._api_base_url}/chat.postMessage",
            headers={"Authorization": f"Bearer {self._token}"},
            json={"channel": self._channel, "text": build_requeue_task_text(context)},
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise SlackAPIError(response.status_code, response.text)

        payload = response.json()
        if not payload.get("ok"):
            raise SlackAPIError(response.status_code, str(payload.get("error") or payload))
        return payload

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=20.0)
        return self._http_client


def build_requeue_task_text(context: RequeueContext) -> str:
    target = f"#{context.pull_request_number}" if context.pull_request_number else f"#{context.issue_number}"
    labels = ", ".join(context.labels) if context.labels else "none"
    return (
        "<!subteam^S0B8X9HTF7A>\n\n"
        "GitHub comment feedback requires Circuit follow-up.\n\n"
        f"Repo: {context.repo}\n"
        f"Issue/PR: {target}\n"
        f"Labels: {labels}\n"
        f"URL: {context.url or 'unavailable'}\n"
        f"Trigger: {context.matched_keyword}\n\n"
        "Comment text:\n"
        f"{context.comment_text}"
    )
