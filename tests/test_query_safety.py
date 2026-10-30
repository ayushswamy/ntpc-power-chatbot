"""
Unit tests for the read-only check in app/db/query.py. No database needed.

Run:  .\\.venv\\Scripts\\python.exe -m pytest tests -q
"""
import pytest

from app.db.query import UnsafeQueryError, check_read_only


@pytest.mark.parametrize("sql", [
    "SELECT TOP 5 * FROM dbo.Master_Plants",
    "select top 1 ReportDate from dbo.XML_RealView where ReportDate = CAST(GETDATE() AS date)",
    "SELECT MAX(UpdatedOn) AS last_update FROM dbo.Data_RealTime_PIData",  # 'UpdatedOn' is not UPDATE
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    "SELECT TOP 5 * FROM dbo.Master_Blocks;",  # one trailing semicolon is fine
])
def test_allowed(sql):
    check_read_only(sql)


@pytest.mark.parametrize("sql", [
    "UPDATE dbo.Master_Plants SET StationName = 'x'",
    "DELETE FROM dbo.Master_Plants",
    "SELECT * INTO dbo.NewTable FROM dbo.Master_Plants",
    "SELECT 1; DROP TABLE dbo.Master_Plants",
    "EXEC sp_who",
    "  insert into t values (1)",
    "SELECT * FROM OPENROWSET('x','y','z')",
])
def test_blocked(sql):
    with pytest.raises(UnsafeQueryError):
        check_read_only(sql)
