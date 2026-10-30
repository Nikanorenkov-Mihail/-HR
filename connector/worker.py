from __future__ import annotations

import asyncio
import logging

from .config import ConnectorSettings
from .opencode import OpenCodeClient, OpenCodeContractChanged, OpenCodeError, OpenCodeUnavailable
from .relay_client import RelayClient, RelayError

log = logging.getLogger(__name__)


async def _prompt_with_lease(job: dict, settings: ConnectorSettings, relay: RelayClient,
                             opencode: OpenCodeClient) -> str:
    prompt_task = asyncio.create_task(opencode.send_message(job))
    try:
        while True:
            done, _ = await asyncio.wait({prompt_task}, timeout=settings.lease_heartbeat_seconds)
            if prompt_task in done:
                return await prompt_task
            try:
                renewed = await relay.heartbeat_job(job["id"])
            except RelayError:
                log.exception("Lost relay while OpenCode was processing job %s", job["id"])
                prompt_task.cancel()
                await asyncio.gather(prompt_task, return_exceptions=True)
                raise
            if not renewed:
                prompt_task.cancel()
                await asyncio.gather(prompt_task, return_exceptions=True)
                raise RelayError("Job lease expired while OpenCode was processing")
    except BaseException:
        if not prompt_task.done():
            prompt_task.cancel()
            await asyncio.gather(prompt_task, return_exceptions=True)
        raise


async def _process_job(job: dict, settings: ConnectorSettings, relay: RelayClient,
                       opencode: OpenCodeClient) -> None:
    try:
        answer = await _prompt_with_lease(job, settings, relay, opencode)
        await relay.complete_job(job["id"], answer)
        log.info("Completed relay job %s", job["id"])
    except OpenCodeContractChanged:
        log.exception("OpenCode API contract changed while processing job %s", job["id"])
        try:
            await relay.fail_job(job["id"], "opencode_api_unsupported")
        except RelayError:
            log.exception("Could not report contract failure for job %s", job["id"])
    except OpenCodeUnavailable:
        log.exception("OpenCode became unavailable while processing job %s", job["id"])
        try:
            await relay.fail_job(job["id"], "processing_interrupted")
        except RelayError:
            log.exception("Could not report interrupted job %s", job["id"])
    except OpenCodeError:
        log.exception("OpenCode failed while processing job %s", job["id"])
        try:
            await relay.fail_job(job["id"], "processing_interrupted")
        except RelayError:
            log.exception("Could not report OpenCode error for job %s", job["id"])
    except RelayError:
        # Do not replay: OpenCode may already have completed a Calendar or task action.
        log.exception("Relay failed after job %s was claimed; it will expire without replay", job["id"])
    except Exception:
        log.exception("Unexpected connector error for job %s", job["id"])
        try:
            await relay.fail_job(job["id"], "processing_interrupted")
        except RelayError:
            log.exception("Could not report failure for job %s", job["id"])


async def run_worker(settings: ConnectorSettings, relay: RelayClient, opencode: OpenCodeClient) -> None:
    last_check = 0.0
    while True:
        try:
            now = asyncio.get_running_loop().time()
            if now - last_check >= settings.opencode_check_interval:
                version = await opencode.validate_runtime(force=opencode.version is None)
                last_check = now
                await relay.heartbeat(healthy=True, opencode_version=version)

            job = await relay.claim_job()
            if job is not None:
                if not isinstance(job.get("id"), str) or not isinstance(job.get("prompt"), str):
                    log.error("Relay returned a malformed job; ignoring it")
                    continue
                await _process_job(job, settings, relay, opencode)
        except OpenCodeUnavailable:
            log.exception("OpenCode is unavailable; connector will keep retrying")
            try:
                await relay.heartbeat(healthy=False, opencode_version=opencode.version)
            except RelayError:
                log.exception("Could not report OpenCode health to relay")
            last_check = 0
            await asyncio.sleep(5)
        except OpenCodeContractChanged:
            log.exception("OpenCode version/API is not compatible with this connector; queue will remain paused")
            try:
                await relay.heartbeat(healthy=False, opencode_version=opencode.version)
            except RelayError:
                log.exception("Could not report incompatible OpenCode API to relay")
            last_check = 0
            await asyncio.sleep(15)
        except RelayError:
            log.exception("VPS relay is unavailable; connector will retry")
            await asyncio.sleep(5)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    from pathlib import Path

    from dotenv import load_dotenv

    from .config import load_settings

    load_dotenv(Path(__file__).with_name(".env"))
    settings = load_settings()
    relay = RelayClient(settings)
    opencode = OpenCodeClient(settings)
    try:
        await run_worker(settings, relay, opencode)
    finally:
        await relay.close()
        await opencode.close()


if __name__ == "__main__":
    asyncio.run(main())
