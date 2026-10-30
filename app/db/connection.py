"""
Opens connections to the NTPC SQL Server (DB_PlantLive).

- Credentials come only from .env (via app.config).
- Every query on a connection times out after QUERY_TIMEOUT_SECONDS (30 s).
- Error messages are cleaned so the password can never leak into logs.

This module only opens connections. The safe query runner (SELECT-only checks,
row limits, logging) is built on top of it in Phase 2.
"""
import pyodbc

from app.config import settings


class DatabaseError(Exception):
    """Raised with a message that is safe to print (no credentials)."""


def _odbc_value(value: str) -> str:
    """Wrap a value in {} so characters like ; or = in a password don't break the string."""
    return "{" + value.replace("}", "}}") + "}"


def _connection_string() -> str:
    return (
        f"DRIVER={_odbc_value(settings.db_driver)};"
        f"SERVER={settings.db_server};"
        f"DATABASE={settings.db_name};"
        f"UID={_odbc_value(settings.db_user)};"
        f"PWD={_odbc_value(settings.db_password)};"
        "TrustServerCertificate=yes;"
        "ApplicationIntent=ReadOnly;"
        "APP=NTPC-Power-Chatbot;"  # shows up in SQL Server monitoring, so DBAs can identify us
    )


def _clean(message: str) -> str:
    """Remove the user name and password from an error message, just in case."""
    for secret in (settings.db_password, settings.db_user):
        if secret:
            message = message.replace(secret, "***")
    return message


def get_connection() -> pyodbc.Connection:
    """
    Return an open connection. Use it with `with` or close it yourself:

        conn = get_connection()
        try:
            ...
        finally:
            conn.close()
    """
    missing = settings.missing_db_settings()
    if missing:
        raise DatabaseError(f"Missing settings in .env: {', '.join(missing)}")
    try:
        # autocommit=True: no transaction is left open, so we never hold locks longer than one query
        conn = pyodbc.connect(_connection_string(), timeout=settings.db_login_timeout, autocommit=True)
    except pyodbc.Error as exc:
        raise DatabaseError(_clean(f"Could not connect to {settings.db_server}: {exc}")) from None
    conn.timeout = settings.query_timeout  # per-query timeout in seconds
    return conn
