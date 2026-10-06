"""Liveness and dependency-readiness routes."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from loguru import logger

from app.core.config import settings
from app.database.connection import get_db_connection

router = APIRouter(tags=["health"])

_DEPLOYMENT_SHA_FILE = Path(__file__).resolve().parents[2] / "deployment_sha.txt"

_REQUIRED_TABLES = (
    "reference_ranges",
    "experiment_runs",
    "control_cycles",
    "system_logs",
    "system_settings",
    "notification_logs",
)
_REQUIRED_STATUS_WIDTHS = {
    "control_cycles": 64,
    "system_logs": 64,
}
_REQUIRED_NOTIFICATION_COLUMNS = {
    "provider_message_id",
    "latest_provider_response",
    "provider_status_updated_at",
    "status_checked_at",
    "status_check_count",
}


class _ReadinessError(RuntimeError):
    """A safe, operator-facing category for an incomplete database schema."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def deployment_sha() -> str:
    """Return the build identity recorded in the release artifact."""
    try:
        marker = _DEPLOYMENT_SHA_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        marker = ""
    return marker or "unknown"


@router.get("/")
def read_root() -> dict[str, str]:
    """Return backend health and deployment identity."""
    return {
        "app": settings.app_name,
        "name": settings.app_full_name,
        "version": settings.app_version,
        "app_env": settings.app_env,
        "db_schema": settings.db_schema,
        "build_sha": deployment_sha(),
        "build_label": settings.build_label,
        "message": f"{settings.app_name} backend is running",
    }


@router.get("/health/ready")
def read_ready() -> dict[str, str]:
    """Confirm PostgreSQL connectivity and the required runtime schema."""
    qualified_tables = tuple(f"{settings.db_schema}.{table}" for table in _REQUIRED_TABLES)
    placeholders = ", ".join("to_regclass(%s)" for _ in qualified_tables)

    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT {placeholders};", qualified_tables)
                resolved_tables = cursor.fetchone()

                cursor.execute(
                    """
                    SELECT table_name, character_maximum_length
                    FROM information_schema.columns
                    WHERE table_schema = %s
                      AND column_name = 'status'
                      AND table_name = ANY(%s);
                    """,
                    (settings.db_schema, list(_REQUIRED_STATUS_WIDTHS)),
                )
                status_widths = dict(cursor.fetchall())

                cursor.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = %s
                      AND table_name = 'notification_logs'
                      AND column_name = ANY(%s);
                    """,
                    (settings.db_schema, list(_REQUIRED_NOTIFICATION_COLUMNS)),
                )
                notification_columns = {row[0] for row in cursor.fetchall()}

        if not resolved_tables or any(table is None for table in resolved_tables):
            missing = [
                table
                for table, resolved in zip(qualified_tables, resolved_tables or (), strict=False)
                if resolved is None
            ]
            raise _ReadinessError(
                "schema_missing_tables",
                f"Missing required tables: {', '.join(missing) or 'unknown'}",
            )

        undersized_columns = [
            f"{table}.status"
            for table, minimum_width in _REQUIRED_STATUS_WIDTHS.items()
            if (status_widths.get(table) or 0) < minimum_width
        ]
        if undersized_columns:
            raise _ReadinessError(
                "schema_status_columns_outdated",
                f"Status columns require migration: {', '.join(undersized_columns)}"
            )

        missing_notification_columns = sorted(
            _REQUIRED_NOTIFICATION_COLUMNS - notification_columns
        )
        if missing_notification_columns:
            raise _ReadinessError(
                "schema_notification_columns_outdated",
                "Notification columns require migration: "
                f"{', '.join(missing_notification_columns)}"
            )
    except _ReadinessError as exc:
        logger.error("Backend readiness check failed: {}", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Backend database schema is not ready.",
                "reason": exc.reason,
            },
        ) from exc
    except Exception as exc:
        logger.error("Backend readiness check failed: {}", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Backend database connection is not ready.",
                "reason": "database_connection_failed",
            },
        ) from exc

    return {
        "app": settings.app_name,
        "status": "ready",
        "app_env": settings.app_env,
        "db_schema": settings.db_schema,
        "build_sha": deployment_sha(),
    }
