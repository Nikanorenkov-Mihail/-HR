from __future__ import annotations

import asyncio
import logging
import uuid

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from .settings import Settings
from .store import RelayStore

log = logging.getLogger(__name__)
router = Router()
JOB_NAMESPACE = uuid.UUID("ae637f53-1e3b-4a8b-9caa-8c8a2e871c88")


def is_authorized(message: Message, settings: Settings) -> bool:
    return bool(
        message.from_user
        and message.chat.type == "private"
        and message.from_user.id == settings.authorized_user_id
        and message.chat.id == settings.authorized_user_id
    )


@router.message(CommandStart())
async def start_command(message: Message, settings: Settings) -> None:
    if not is_authorized(message, settings):
        await message.answer("Доступ к этому боту не настроен.")
        return
    await message.answer("Напиши запрос — передам его OpenCode на рабочем ПК.")


@router.message(Command("help"))
async def help_command(message: Message, settings: Settings) -> None:
    if not is_authorized(message, settings):
        await message.answer("Доступ к этому боту не настроен.")
        return
    await message.answer("Отправь сообщение, чтобы передать его агенту. Ответ появится вместо «Размышление».")


@router.message(F.text & ~F.text.startswith("/"))
async def relay_text(message: Message, settings: Settings, store: RelayStore) -> None:
    if not is_authorized(message, settings):
        await message.answer("Доступ к этому боту не настроен.")
        return
    text = (message.text or "").strip()
    if not text:
        return

    thinking = await message.answer("Размышление")
    job_id = uuid.uuid5(JOB_NAMESPACE, f"{message.chat.id}:{message.message_id}").hex
    try:
        store.enqueue_job(
            job_id=job_id,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            message_id=thinking.message_id,
            prompt_text=text,
        )
    except Exception:
        log.exception("Could not enqueue Telegram request")
        await safe_edit(message.bot, message.chat.id, thinking.message_id, "Не удалось поставить запрос в очередь.")


async def safe_edit(bot: Bot, chat_id: int, message_id: int, text: str) -> None:
    try:
        await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def split_for_telegram(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


async def deliver_job_reply(bot: Bot, job: dict, settings: Settings, store: RelayStore) -> None:
    if job["status"] == "complete":
        reply = (job.get("result_text") or "Агент вернул пустой ответ.").strip()
    else:
        reply = {
            "agent_unavailable": "Агент на рабочем ПК сейчас недоступен. Запрос не был передан.",
            "opencode_api_unsupported": "Версия OpenCode изменила API. Обработка остановлена; обновите connector.",
            "processing_interrupted": (
                "Связь с OpenCode прервалась во время обработки. Проверь результат у агента "
                "перед повторной отправкой: действие могло выполниться."
            ),
            "opencode_timeout": (
                "OpenCode не вернул ответ вовремя. Проверь состояние у агента перед повторной отправкой: "
                "действие могло выполниться."
            ),
            "opencode_error": "OpenCode не смог обработать запрос. Подробности записаны в журнал connector-а.",
        }.get(job.get("error_code"), "Не удалось обработать запрос. Повтори его позже.")

    parts = split_for_telegram(reply or "Агент вернул пустой ответ.")
    await safe_edit(bot, job["chat_id"], job["message_id"], parts[0])
    for part in parts[1:]:
        await bot.send_message(chat_id=job["chat_id"], text=part)
    store.mark_reply_sent(job["id"])


async def delivery_loop(bot: Bot, settings: Settings, store: RelayStore) -> None:
    while True:
        for job in store.pending_replies():
            try:
                await deliver_job_reply(bot, job, settings, store)
            except TelegramRetryAfter as exc:
                store.retry_reply(job["id"], job["reply_attempts"] + 1, settings.max_delivery_attempts)
                await asyncio.sleep(min(exc.retry_after, 30))
            except Exception:
                log.exception("Telegram reply delivery failed for job %s", job["id"])
                store.retry_reply(job["id"], job["reply_attempts"] + 1, settings.max_delivery_attempts)

        for item in store.claim_notifications():
            try:
                await bot.send_message(chat_id=item["chat_id"], text=item["text"])
                store.finish_notification(item["operation_id"], "sent")
            except TelegramRetryAfter as exc:
                store.retry_notification(
                    item["operation_id"], item["attempts"] + 1, settings.max_delivery_attempts
                )
                await asyncio.sleep(min(exc.retry_after, 30))
            except TelegramBadRequest as exc:
                log.warning("Telegram rejected reminder %s: %s", item["operation_id"], exc)
                store.finish_notification(item["operation_id"], "failed")
            except Exception:
                # Delivery may have succeeded even if its HTTP response was lost.
                # Mark unknown rather than risk sending a duplicate reminder.
                log.exception("Reminder delivery status is uncertain for %s", item["operation_id"])
                store.finish_notification(item["operation_id"], "unknown")

        store.prune_old_jobs()
        await asyncio.sleep(settings.delivery_poll_seconds)
