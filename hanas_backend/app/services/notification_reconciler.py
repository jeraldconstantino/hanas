"""Background reconciliation of nonterminal Semaphore notification statuses."""

from __future__ import annotations

import asyncio
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from loguru import logger
from psycopg2.extensions import connection as PsycopgConnection

from app.core.config import settings
from app.database.connection import get_db_connection
from app.database.repositories.sensor_repository import PostgresSensorReadingRepository
from app.services.notifications import _parse_semaphore_message


NOTIFICATION_RECONCILER_LOCK_KEY = 340_240_502


async def run_notification_status_loop() -> None:
    """Reconcile pending provider statuses without blocking sensor requests."""
    logger.info(
        "SMS status reconciler started (interval={}s, max_checks={}).",
        settings.sms_status_poll_interval_seconds,
        settings.sms_status_max_checks,
    )
    while True:
        try:
            await asyncio.to_thread(reconcile_pending_notification_statuses)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # A database or provider failure must not permanently stop later checks.
            logger.exception("SMS status reconciliation cycle failed: {}", exc)
        await asyncio.sleep(max(settings.sms_status_poll_interval_seconds, 5))


def reconcile_pending_notification_statuses() -> int:
    """Fetch and persist current Semaphore states for recent pending messages."""
    if not settings.semaphore_api_key:
        return 0

    updated = 0
    with get_db_connection() as db_connection:
        if not _try_acquire_reconciler_lock(db_connection):
            return 0
        try:
            repository = PostgresSensorReadingRepository(db_connection)
            pending = repository.get_pending_notification_status_checks(
                limit=settings.sms_status_poll_batch_size,
                max_age_days=settings.sms_status_lookback_days,
                max_checks=settings.sms_status_max_checks,
                min_check_interval_seconds=max(settings.sms_status_poll_interval_seconds, 5),
            )
            for notification_log_id, provider_message_id in pending:
                try:
                    response_body = _fetch_semaphore_message(provider_message_id)
                    returned_message_id, status = _parse_semaphore_message(response_body)
                    if returned_message_id != provider_message_id or status is None:
                        raise ValueError(
                            "Semaphore status response did not identify the requested message"
                        )
                except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                    repository.record_notification_status_check_failure(notification_log_id)
                    logger.warning(
                        "SMS status check failed for provider_message_id={}: {}",
                        provider_message_id,
                        exc,
                    )
                    continue

                repository.update_notification_provider_status(
                    notification_log_id,
                    status=status,
                    provider_response=response_body,
                )
                updated += 1
                logger.info(
                    "SMS provider status reconciled: provider_message_id={} status={}",
                    provider_message_id,
                    status,
                )
        finally:
            _release_reconciler_lock(db_connection)
    return updated


def _fetch_semaphore_message(message_id: int) -> str:
    """Retrieve one Semaphore message without exposing the API key in logs."""
    query = urlencode({"apikey": settings.semaphore_api_key})
    request = Request(
        f"https://api.semaphore.co/api/v4/messages/{message_id}?{query}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8", errors="replace").strip()


def _try_acquire_reconciler_lock(db_connection: PsycopgConnection) -> bool:
    """Allow only one backend worker to poll Semaphore at a time."""
    with db_connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s);", (NOTIFICATION_RECONCILER_LOCK_KEY,))
        row = cursor.fetchone()
    return bool(row and row[0])


def _release_reconciler_lock(db_connection: PsycopgConnection) -> None:
    """Release the cross-worker status-reconciliation lock."""
    with db_connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s);", (NOTIFICATION_RECONCILER_LOCK_KEY,))
