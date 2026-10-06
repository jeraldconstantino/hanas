"""Connection setup failures must release database resources."""

import pytest

from app.database import connection


@pytest.mark.parametrize("fail_setup", [False, True])
def test_database_connection_closes_after_setup_or_request_failure(monkeypatch, fail_setup):
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args):
            if fail_setup:
                raise RuntimeError("setup failed")

    class Connection:
        closed = False
        def cursor(self): return Cursor()
        def close(self): self.closed = True

    database = Connection()
    options = {}

    def connect(**kwargs):
        options.update(kwargs)
        return database

    monkeypatch.setattr(connection, "load_db_config", lambda: {"dbname": "isolated-test"})
    monkeypatch.setattr(connection.psycopg2, "connect", connect)
    with pytest.raises(RuntimeError, match="setup failed" if fail_setup else "request failed"):
        with connection.get_db_connection():
            raise RuntimeError("request failed")
    assert database.closed
    assert options["connect_timeout"] == 10
