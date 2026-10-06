"""Validate the current schema; fresh installs use sql/init_dev.sql or init_prod.sql."""

from app.api.routes.health import read_ready


def ensure_runtime_schema() -> None:
    """Reject an incomplete schema without migrating historical databases."""
    read_ready()
