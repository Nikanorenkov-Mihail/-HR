from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
from typing import Iterator


class RelayStore:
    """Short-lived transport state only; this is not the agent's task memory."""

    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    prompt_text TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    worker_id TEXT,
                    lease_until REAL,
                    result_text TEXT,
                    error_code TEXT,
                    reply_state TEXT NOT NULL DEFAULT 'pending',
                    reply_attempts INTEGER NOT NULL DEFAULT 0,
                    next_reply_attempt REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_pending_idx
                    ON jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS jobs_reply_idx
                    ON jobs(reply_state, next_reply_attempt);

                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY,
                    healthy INTEGER NOT NULL,
                    opencode_version TEXT,
                    last_seen REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    operation_id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS notifications_pending_idx
                    ON notifications(status, next_attempt);
                """
            )
            # A send may have reached Telegram before the process stopped. Do not
            # resend it automatically: that could produce duplicate reminders.
            conn.execute(
                "UPDATE notifications SET status = 'unknown', updated_at = ? WHERE status = 'sending'",
                (time.time(),),
            )
            # A claimed OpenCode call may have completed a Calendar action before
            # the connector disappeared. Never replay it automatically.
            now = time.time()
            conn.execute(
                """UPDATE jobs SET status = 'failed', error_code = 'processing_interrupted',
                   prompt_text = NULL, reply_state = 'pending', updated_at = ?
                   WHERE status = 'claimed' AND lease_until < ?""",
                (now, now),
            )

    def enqueue_job(
        self, job_id: str, user_id: int, chat_id: int, message_id: int, prompt_text: str
    ) -> bool:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO jobs
                   (id, user_id, chat_id, message_id, prompt_text, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (job_id, user_id, chat_id, message_id, prompt_text, now, now),
            )
            return cur.rowcount == 1

    def worker_heartbeat(self, worker_id: str, healthy: bool, version: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO workers(worker_id, healthy, opencode_version, last_seen)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(worker_id) DO UPDATE SET
                     healthy = excluded.healthy,
                     opencode_version = excluded.opencode_version,
                     last_seen = excluded.last_seen""",
                (worker_id, int(healthy), version, time.time()),
            )

    def claim_job(self, worker_id: str, lease_seconds: int, pending_ttl_seconds: int) -> dict | None:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE jobs SET status = 'failed', error_code = 'agent_unavailable',
                   prompt_text = NULL, reply_state = 'pending', updated_at = ?
                   WHERE status = 'pending' AND created_at < ?""",
                (now, now - pending_ttl_seconds),
            )
            conn.execute(
                """UPDATE jobs SET status = 'failed', error_code = 'processing_interrupted',
                   prompt_text = NULL, reply_state = 'pending', updated_at = ?
                   WHERE status = 'claimed' AND lease_until < ?""",
                (now, now),
            )
            worker = conn.execute(
                "SELECT healthy FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if not worker or not worker["healthy"]:
                return None
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = 'pending' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """UPDATE jobs SET status = 'claimed', worker_id = ?, lease_until = ?, updated_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (worker_id, now + lease_seconds, now, row["id"]),
            )
            return dict(row)

    def heartbeat_job(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE jobs SET lease_until = ?, updated_at = ?
                   WHERE id = ? AND worker_id = ? AND status = 'claimed'""",
                (now + lease_seconds, now, job_id, worker_id),
            )
            return cur.rowcount == 1

    def complete_job(self, job_id: str, worker_id: str, result_text: str) -> bool:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE jobs SET status = 'complete', prompt_text = NULL, result_text = ?,
                   error_code = NULL, lease_until = NULL, updated_at = ?
                   WHERE id = ? AND worker_id = ? AND status = 'claimed'""",
                (result_text, now, job_id, worker_id),
            )
            return cur.rowcount == 1

    def fail_job(self, job_id: str, worker_id: str, error_code: str) -> bool:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE jobs SET status = 'failed', prompt_text = NULL, result_text = NULL,
                   error_code = ?, lease_until = NULL, updated_at = ?
                   WHERE id = ? AND worker_id = ? AND status = 'claimed'""",
                (error_code, now, job_id, worker_id),
            )
            return cur.rowcount == 1

    def pending_replies(self, limit: int = 20) -> list[dict]:
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM jobs WHERE reply_state = 'pending' AND status IN ('complete', 'failed')
                   AND next_reply_attempt <= ? ORDER BY updated_at LIMIT ?""",
                (now, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_reply_sent(self, job_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE jobs SET reply_state = 'sent', result_text = NULL,
                   error_code = NULL, updated_at = ? WHERE id = ?""",
                (time.time(), job_id),
            )

    def retry_reply(self, job_id: str, attempts: int, max_attempts: int) -> None:
        now = time.time()
        state = "blocked" if attempts >= max_attempts else "pending"
        delay = min(300, 2 ** min(attempts, 8))
        with self._connect() as conn:
            conn.execute(
                """UPDATE jobs SET reply_state = ?, reply_attempts = ?, next_reply_attempt = ?,
                   updated_at = ? WHERE id = ?""",
                (state, attempts, now + delay, now, job_id),
            )

    def enqueue_notification(self, operation_id: str, chat_id: int, text: str) -> tuple[dict, bool]:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO notifications
                   (operation_id, chat_id, text, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (operation_id, chat_id, text, now, now),
            )
            row = conn.execute(
                "SELECT * FROM notifications WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if row["chat_id"] != chat_id or row["text"] != text:
                raise ValueError("operation_id was already used with different notification data")
            return dict(row), cur.rowcount == 1

    def claim_notifications(self, limit: int = 10) -> list[dict]:
        now = time.time()
        claimed: list[dict] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT * FROM notifications WHERE status = 'pending' AND next_attempt <= ?
                   ORDER BY created_at LIMIT ?""",
                (now, limit),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE notifications SET status = 'sending', updated_at = ? WHERE operation_id = ?",
                    (now, row["operation_id"]),
                )
                claimed.append(dict(row))
        return claimed

    def finish_notification(self, operation_id: str, status: str) -> None:
        if status not in {"sent", "failed", "unknown"}:
            raise ValueError("invalid notification status")
        with self._connect() as conn:
            conn.execute(
                "UPDATE notifications SET status = ?, updated_at = ? WHERE operation_id = ?",
                (status, time.time(), operation_id),
            )

    def retry_notification(self, operation_id: str, attempts: int, max_attempts: int) -> None:
        now = time.time()
        status = "failed" if attempts >= max_attempts else "pending"
        delay = min(300, 2 ** min(attempts, 8))
        with self._connect() as conn:
            conn.execute(
                """UPDATE notifications SET status = ?, attempts = ?, next_attempt = ?, updated_at = ?
                   WHERE operation_id = ?""",
                (status, attempts, now + delay, now, operation_id),
            )

    def notification_status(self, operation_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT operation_id, status FROM notifications WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            return dict(row) if row else None

    def queue_stats(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
            return {row["status"]: row["count"] for row in rows}

    def prune_old_jobs(self, older_than_seconds: int = 7 * 24 * 3600) -> None:
        cutoff = time.time() - older_than_seconds
        with self._connect() as conn:
            conn.execute(
                """DELETE FROM jobs WHERE updated_at < ? AND reply_state IN ('sent', 'blocked')
                   AND prompt_text IS NULL AND result_text IS NULL""",
                (cutoff,),
            )
