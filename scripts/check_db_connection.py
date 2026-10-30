"""
Phase 0 - Verify the database connection and that the login is READ-ONLY.

What it does (all read-only; it never tries to write anything):
  1. Connects to DB_PlantLive using the settings in .env.
  2. Reads basic server info (SQL Server version, server clock time).
  3. Checks role membership (sysadmin, db_owner, db_datawriter, db_ddladmin, ...).
  4. Lists the login's effective permissions at server, database, schema and
     object level (using SQL Server's built-in fn_my_permissions).
  5. Runs one tiny SELECT (count of Master_Blocks, 96 rows) to prove reading works.
  6. Prints a PASS / WARNING / FAIL verdict and saves the report to outputs/phase0/.

Run it from the project folder:
    .\\.venv\\Scripts\\python.exe scripts\\check_db_connection.py
"""
import sys
from datetime import datetime
from pathlib import Path

# Let this script import the "app" package when run from the project folder
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import OUTPUTS_DIR, settings  # noqa: E402
from app.db.connection import DatabaseError, get_connection  # noqa: E402

# Any permission containing one of these words could change data or the database
WRITE_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "ALTER", "CREATE", "DROP", "CONTROL",
    "TAKE OWNERSHIP", "IMPERSONATE", "BACKUP", "ADMINISTER", "KILL",
    "SHUTDOWN", "UNSAFE ASSEMBLY", "EXTERNAL ACCESS", "AUTHENTICATE", "CHECKPOINT",
)

# All queries are fixed, read-only metadata/permission lookups
SQL_SERVER_INFO = """
SELECT DB_NAME() AS database_name,
       CAST(SERVERPROPERTY('ProductVersion') AS varchar(50)) AS sql_version,
       CAST(SERVERPROPERTY('Edition') AS varchar(100)) AS edition,
       SYSDATETIME() AS server_time
"""

SQL_ROLES = """
SELECT IS_SRVROLEMEMBER('sysadmin')   AS sysadmin,
       IS_MEMBER('db_owner')          AS db_owner,
       IS_MEMBER('db_datawriter')     AS db_datawriter,
       IS_MEMBER('db_ddladmin')       AS db_ddladmin,
       IS_MEMBER('db_securityadmin')  AS db_securityadmin,
       IS_MEMBER('db_datareader')     AS db_datareader,
       IS_MEMBER('db_denydatawriter') AS db_denydatawriter
"""

SQL_SERVER_PERMS = "SELECT permission_name FROM fn_my_permissions(NULL, 'SERVER')"
SQL_DATABASE_PERMS = "SELECT permission_name FROM fn_my_permissions(NULL, 'DATABASE')"
SQL_SCHEMA_PERMS = "SELECT permission_name FROM fn_my_permissions('dbo', 'SCHEMA')"

# Effective permissions on every table, view and stored procedure the login can see
SQL_OBJECT_PERMS = """
SELECT s.name + '.' + o.name AS object_name, o.type_desc, p.permission_name
FROM sys.objects o
JOIN sys.schemas s ON s.schema_id = o.schema_id
CROSS APPLY fn_my_permissions(QUOTENAME(s.name) + '.' + QUOTENAME(o.name), 'OBJECT') p
WHERE o.type IN ('U', 'V', 'P') AND o.is_ms_shipped = 0 AND p.subentity_name = ''
"""

SQL_READ_TEST = "SELECT COUNT(*) AS row_count FROM dbo.Master_Blocks"


def is_write_permission(name: str) -> bool:
    return any(word in name.upper() for word in WRITE_KEYWORDS)


def main() -> int:
    lines: list[str] = []

    def out(text: str = "") -> None:
        print(text)
        lines.append(text)

    out(f"Phase 0 DB check - {datetime.now():%Y-%m-%d %H:%M:%S}")
    out(f"Server: {settings.db_server}   Database: {settings.db_name}   Driver: {settings.db_driver}")
    out("(login name and password are not shown)")
    out()

    try:
        conn = get_connection()
    except DatabaseError as exc:
        out(f"CONNECTION FAILED: {exc}")
        save_report(lines)
        return 2

    problems: list[str] = []  # anything that means the login can write
    warnings: list[str] = []  # things worth knowing but not direct write access
    try:
        cur = conn.cursor()

        # 1. Server info
        row = cur.execute(SQL_SERVER_INFO).fetchone()
        out(f"Connected OK. database={row.database_name}  SQL Server {row.sql_version} ({row.edition})")
        out(f"Server clock: {row.server_time}   This PC clock: {datetime.now()}")
        out()

        # 2. Role membership (1 = member, 0 = not, None = role not found)
        roles = cur.execute(SQL_ROLES).fetchone()
        out("Role membership (1 = yes):")
        for col in [d[0] for d in cur.description]:
            value = getattr(roles, col)
            out(f"  {col:<18} {value}")
            if col in ("sysadmin", "db_owner", "db_datawriter", "db_ddladmin", "db_securityadmin") and value == 1:
                problems.append(f"member of role {col}")
        out()

        # 3-5. Server / database / schema permissions
        for level, sql in (("SERVER", SQL_SERVER_PERMS), ("DATABASE", SQL_DATABASE_PERMS), ("SCHEMA dbo", SQL_SCHEMA_PERMS)):
            perms = sorted({r.permission_name for r in cur.execute(sql).fetchall()})
            out(f"{level} permissions: {', '.join(perms) if perms else '(none)'}")
            for p in perms:
                if is_write_permission(p):
                    problems.append(f"{level}: {p}")
                elif p == "EXECUTE":
                    warnings.append(f"{level}: EXECUTE (stored procedures could modify data; the chatbot never calls them)")
        out()

        # 6. Object-level permissions
        rows = cur.execute(SQL_OBJECT_PERMS).fetchall()
        per_object: dict[str, set[str]] = {}
        for r in rows:
            per_object.setdefault(f"{r.object_name} ({r.type_desc})", set()).add(r.permission_name)
        out(f"Object permissions: {len(per_object)} objects visible")
        summary: dict[str, int] = {}
        for obj, perms in sorted(per_object.items()):
            for p in perms:
                summary[p] = summary.get(p, 0) + 1
                if is_write_permission(p):
                    problems.append(f"{obj}: {p}")
                elif p == "EXECUTE":
                    warnings.append(f"{obj}: EXECUTE")
        for p, count in sorted(summary.items()):
            out(f"  {p:<20} on {count} objects")
        out()

        # 7. Tiny read test
        count = cur.execute(SQL_READ_TEST).fetchone().row_count
        out(f"Read test: Master_Blocks has {count} rows (expected 96)")
        out()
    finally:
        conn.close()

    # Verdict
    if problems:
        out("VERDICT: FAIL - this login CAN WRITE. Do not continue until a read-only login is provided.")
        for p in problems:
            out(f"  - {p}")
        code = 1
    elif warnings:
        out("VERDICT: WARNING - no direct write permission found, but please review:")
        for w in warnings:
            out(f"  - {w}")
        code = 0
    else:
        out("VERDICT: PASS - the login is read-only (SELECT/VIEW permissions only).")
        code = 0

    save_report(lines)
    return code


def save_report(lines: list[str]) -> None:
    folder = OUTPUTS_DIR / "phase0"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"db_check_{datetime.now():%Y%m%d_%H%M%S}.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport saved to {path}")


if __name__ == "__main__":
    sys.exit(main())
