from __future__ import annotations

import aiohttp

from .config import ConnectorSettings


class RelayError(RuntimeError):
    pass


class RelayClient:
    def __init__(self, settings: ConnectorSettings):
        self.settings = settings
        timeout = aiohttp.ClientTimeout(total=40, connect=10, sock_read=35)
        self.session = aiohttp.ClientSession(
            base_url=settings.relay_base_url,
            headers={
                "Authorization": f"Bearer {settings.relay_token}",
                "User-Agent": "hr-opencode-pc-connector/0.1",
            },
            timeout=timeout,
        )

    async def close(self) -> None:
        await self.session.close()

    async def _request(self, method: str, path: str, **kwargs) -> dict | None:
        try:
            async with self.session.request(method, path, **kwargs) as response:
                if response.status == 204:
                    return None
                if response.status == 409:
                    raise RelayError("Relay rejected a stale job lease")
                if response.status == 401:
                    raise RelayError("Relay authentication failed; check RELAY_TOKEN")
                if response.status >= 400:
                    raise RelayError(f"Relay returned HTTP {response.status}")
                data = await response.json(content_type=None)
                if not isinstance(data, dict):
                    raise RelayError("Relay returned an unexpected response shape")
                return data
        except aiohttp.ClientError as exc:
            raise RelayError(f"Could not reach relay: {exc}") from exc

    async def heartbeat(self, healthy: bool, opencode_version: str | None) -> None:
        await self._request(
            "POST",
            "/internal/v1/agent/heartbeat",
            json={
                "worker_id": self.settings.worker_id,
                "healthy": healthy,
                "opencode_version": opencode_version,
            },
        )

    async def claim_job(self) -> dict | None:
        return await self._request(
            "POST",
            "/internal/v1/jobs/claim",
            json={"worker_id": self.settings.worker_id, "wait_seconds": self.settings.poll_wait_seconds},
        )

    async def heartbeat_job(self, job_id: str) -> bool:
        result = await self._request(
            "POST",
            f"/internal/v1/jobs/{job_id}/heartbeat",
            json={"worker_id": self.settings.worker_id},
        )
        return bool(result and result.get("accepted"))

    async def complete_job(self, job_id: str, result: str) -> None:
        await self._request(
            "POST",
            f"/internal/v1/jobs/{job_id}/complete",
            json={"worker_id": self.settings.worker_id, "result": result},
        )

    async def fail_job(self, job_id: str, error_code: str) -> None:
        await self._request(
            "POST",
            f"/internal/v1/jobs/{job_id}/fail",
            json={"worker_id": self.settings.worker_id, "error_code": error_code},
        )
