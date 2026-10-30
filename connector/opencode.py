from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import logging

import aiohttp
from aiohttp import BasicAuth

from .config import ConnectorSettings

log = logging.getLogger(__name__)


class OpenCodeError(RuntimeError):
    pass


class OpenCodeUnavailable(OpenCodeError):
    pass


class OpenCodeContractChanged(OpenCodeError):
    pass


class OpenCodeResponseError(OpenCodeError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self.chunks.append(data)


class OpenCodeClient:
    """Small REST adapter with runtime contract checks for frequently updated OpenCode."""

    def __init__(self, settings: ConnectorSettings):
        self.settings = settings
        timeout = aiohttp.ClientTimeout(total=settings.request_timeout, connect=10, sock_read=settings.request_timeout)
        self.session = aiohttp.ClientSession(
            base_url=settings.opencode_base_url,
            auth=BasicAuth("opencode", settings.opencode_password),
            timeout=timeout,
            headers={"User-Agent": "hr-telegram-opencode-connector/0.1"},
        )
        self.version: str | None = None
        self.agent_validated = False
        self.session_id: str | None = settings.opencode_session_id or None

    async def close(self) -> None:
        await self.session.close()

    async def _json(self, method: str, path: str, **kwargs) -> dict | list:
        try:
            async with self.session.request(method, path, **kwargs) as response:
                if response.status in {401, 403}:
                    raise OpenCodeUnavailable("OpenCode authentication failed")
                if response.status == 404:
                    raise OpenCodeContractChanged(f"OpenCode endpoint not found: {method} {path}")
                if response.status >= 500:
                    raise OpenCodeUnavailable(f"OpenCode server error: HTTP {response.status}")
                if response.status >= 400:
                    # Avoid putting server response bodies in connector logs; they may echo HR data.
                    raise OpenCodeResponseError(f"OpenCode rejected request: HTTP {response.status}")
                data = await response.json(content_type=None)
                if not isinstance(data, (dict, list)):
                    raise OpenCodeResponseError("OpenCode returned an unexpected response shape")
                return data
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
            raise OpenCodeUnavailable("OpenCode server is unreachable") from exc

    async def validate_runtime(self, force: bool = False) -> str:
        health = await self._json("GET", "/global/health")
        if not isinstance(health, dict) or health.get("healthy") is not True:
            raise OpenCodeUnavailable("OpenCode health check failed")
        version = health.get("version")
        if not isinstance(version, str) or not version:
            raise OpenCodeContractChanged("OpenCode health response has no version")

        if force or version != self.version:
            await self._check_live_api_contract()
            agents = await self._json("GET", "/agent")
            if not isinstance(agents, list):
                raise OpenCodeContractChanged("OpenCode /agent response is not a list")
            agent_names = {
                item.get("name", item.get("id"))
                for item in agents
                if isinstance(item, dict)
            }
            if self.settings.opencode_agent not in agent_names:
                raise OpenCodeContractChanged(
                    f"Configured OpenCode agent '{self.settings.opencode_agent}' is missing"
                )
            self.agent_validated = True
            log.info("OpenCode API contract verified; server version=%s, agent=%s", version, self.settings.opencode_agent)
        self.version = version
        await self._ensure_session()
        return version

    async def _check_live_api_contract(self) -> None:
        try:
            async with self.session.get("/doc") as response:
                if response.status != 200:
                    raise OpenCodeContractChanged(f"OpenCode /doc returned HTTP {response.status}")
                raw = await response.text()
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
            raise OpenCodeUnavailable("Could not read OpenCode's live API specification") from exc

        parser = _TextExtractor()
        parser.feed(raw)
        content = " ".join(parser.chunks + [raw]).lower()
        markers = (
            "/session/{id}/message",
            "/session/:id/message",
            "/session/{sessionid}/message",
            "/session/:sessionid/message",
        )
        if not any(marker in content.replace("%7b", "{").replace("%7d", "}") for marker in markers):
            raise OpenCodeContractChanged(
                "OpenCode live API spec no longer advertises POST /session/:id/message"
            )

    async def _ensure_session(self) -> str:
        if self.session_id:
            return self.session_id
        path = self.settings.opencode_session_file
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            stored = path.read_text(encoding="utf-8").strip()
            if stored:
                try:
                    session = await self._json("GET", f"/session/{stored}")
                    if isinstance(session, dict) and session.get("id"):
                        self.session_id = session["id"]
                        return self.session_id
                except OpenCodeContractChanged as exc:
                    # A missing saved session is recoverable; a missing endpoint is not.
                    if f"GET /session/{stored}" not in str(exc):
                        raise
                except OpenCodeResponseError:
                    pass

        created = await self._json("POST", "/session", json={"title": "HR Telegram"})
        if not isinstance(created, dict) or not isinstance(created.get("id"), str):
            raise OpenCodeContractChanged("OpenCode session creation response has no session id")
        session_id = created["id"]
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(session_id, encoding="utf-8")
        tmp.replace(path)
        self.session_id = session_id
        log.info("Using persistent OpenCode session %s", session_id)
        return session_id

    async def send_message(self, job: dict) -> str:
        if not self.agent_validated:
            await self.validate_runtime(force=True)
        session_id = await self._ensure_session()
        text = (
            "[Доверенные данные relay]\n"
            f"Request ID: {job['id']}\n"
            f"Telegram user ID: {job['telegram_user_id']}\n"
            f"Telegram chat ID: {job['telegram_chat_id']}\n"
            "\n[Сообщение пользователя]\n"
            f"{job['prompt']}"
        )
        result = await self._json(
            "POST",
            f"/session/{session_id}/message",
            json={
                "agent": self.settings.opencode_agent,
                "parts": [{"type": "text", "text": text}],
            },
        )
        if not isinstance(result, dict) or not isinstance(result.get("parts"), list):
            raise OpenCodeContractChanged("OpenCode message response no longer contains parts[]")
        text_parts = [
            part.get("text", "")
            for part in result["parts"]
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        ]
        response = "".join(text_parts).strip()
        if not response:
            raise OpenCodeResponseError("OpenCode returned no user-facing text")
        return response
