"""PostgreSQL connection helpers."""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg2
from psycopg2.extensions import connection

from app.core.config import load_db_config, settings


@contextmanager
def get_db_connection() -> Iterator[connection]:
    """Yield a PostgreSQL connection and close it after use."""
    db_config = {key: value for key, value in load_db_config().items() if value}
    db_config.setdefault("connect_timeout", 10)
    db_connection = psycopg2.connect(**db_config)
    try:
        with db_connection.cursor() as cursor:
            cursor.execute("SET TIME ZONE %s;", (settings.app_timezone,))
        yield db_connection
    finally:
        db_connection.close()
