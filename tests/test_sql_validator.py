"""Tests for app/db/sql_validator.py (no database needed)."""
import pytest

from app.db.sql_validator import validate_sql

ALLOWED = {"performance_daycum_station", "master_plants", "xml_realview", "data_outage", "data_trips"}


def check(sql, add_top=False):
    return validate_sql(sql, ALLOWED, max_rows=5000, add_top=add_top)


@pytest.mark.parametrize("sql", [
    "SELECT TOP 10 PlantCode, SUM(DayCY) FROM dbo.Performance_DayCum_Station WHERE ReportDate BETWEEN ? AND ? GROUP BY PlantCode",
    "SELECT TOP 5 * FROM dbo.Master_Plants",
    "SELECT TOP 100 p.StationName, s.DayCY FROM dbo.Performance_DayCum_Station s JOIN dbo.Master_Plants p ON p.PlantCode = s.PlantCode WHERE s.ReportDate = ?",
    "WITH x AS (SELECT PlantCode FROM dbo.Master_Plants) SELECT TOP 5 PlantCode FROM x",
    "SELECT TOP 1 BLK FROM dbo.XML_RealView WHERE ReportDate = ? ORDER BY BLK DESC",
    "SELECT TOP 50 * FROM dbo.Data_Trips WHERE TRP_DT >= ?",
    "SELECT TOP 10 k.PlantCode, SUM(k.v) FROM (SELECT PlantCode, ReportDate, MAX(DayCY) AS v FROM dbo.Performance_DayCum_Station "
    "WHERE ReportDate BETWEEN ? AND ? GROUP BY PlantCode, ReportDate) k GROUP BY k.PlantCode",
])
def test_valid(sql):
    result = check(sql)
    assert result.ok, result.reason


@pytest.mark.parametrize("sql, reason_part", [
    ("UPDATE dbo.Master_Plants SET StationName = 'x'", "Only SELECT"),
    ("DELETE FROM dbo.Master_Plants", "Only SELECT"),
    ("SELECT TOP 5 * INTO dbo.x FROM dbo.Master_Plants", "INTO"),
    ("SELECT TOP 5 * FROM dbo.Master_Plants; DROP TABLE dbo.Master_Plants", "one SQL statement"),
    ("SELECT TOP 5 * FROM dbo.Master_Units", "not in the allowed list"),
    ("SELECT TOP 5 * FROM dbo.Data_Partial WHERE OutageDate = ?", "never"),
    ("SELECT TOP 5 * FROM dbo.Performance_DayCum_Station WHERE PlantCode = '1004'", "reportdate"),
    ("SELECT TOP 5 * FROM dbo.Performance_DayCum_Station", "reportdate"),
    ("SELECT TOP 5 k.v FROM (SELECT DayCY AS v FROM dbo.Performance_DayCum_Station WHERE PlantCode = '1') k WHERE k.v > 0", "reportdate"),
    ("SELECT PlantCode FROM dbo.Master_Plants", "TOP"),
    ("SELECT TOP 99999 PlantCode FROM dbo.Master_Plants", "larger than"),
    ("SELECT TOP 5 * FROM OPENROWSET('SQLNCLI', 'x', 'SELECT 1')", ""),
])
def test_rejected(sql, reason_part):
    result = check(sql)
    assert not result.ok
    assert reason_part.lower() in result.reason.lower()


def test_add_top():
    result = check("SELECT PlantCode FROM dbo.Master_Plants", add_top=True)
    assert result.ok
    assert "TOP 5000" in result.sql.upper()
