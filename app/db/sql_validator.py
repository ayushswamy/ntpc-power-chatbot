"""
SQL validator - checks that a query is safe BEFORE it is sent to SQL Server.

Rules (a query must pass all of them):
  1. Exactly one statement, and it is a SELECT (a WITH ... SELECT is fine).
  2. No SELECT ... INTO, no functions that reach outside the DB (OPENROWSET etc.).
  3. Only tables from the allowed list.
  4. Large tables (Data_Outage, Performance_DayCum_*, XML_RealView, ...) must
     have a condition on their date column in the WHERE clause.
  5. A TOP n row limit is required (add_top=True adds "TOP <max_rows>" if missing).

Uses sqlglot, which really parses the SQL (a keyword search could be fooled).
"""
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

# Date column of each large table (from semantic/tables.yaml)
LARGE_TABLE_DATE_COLUMNS = {
    "performance_daycum_station": "reportdate",
    "performance_daycum_stage": "reportdate",
    "performance_daycum_unit": "reportdate",
    "data_generation": "p_date",
    "data_outage": "outagedate",
    "xml_realview": "reportdate",
}
NEVER_ALLOWED = {"data_partial"}  # no index at all - any query scans 1.3 billion rows

FORBIDDEN_FUNCTIONS = {"openrowset", "openquery", "opendatasource", "openxml"}


@dataclass
class ValidationResult:
    ok: bool
    sql: str            # the (possibly TOP-added) SQL to run
    reason: str = ""


def validate_sql(sql: str, allowed_tables: set[str], max_rows: int, add_top: bool = False) -> ValidationResult:
    """Check `sql`. allowed_tables: lower-case table names without schema."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="tsql") if s is not None]
    except sqlglot.errors.ParseError as exc:
        return ValidationResult(False, sql, f"SQL could not be parsed: {str(exc).splitlines()[0]}")
    if len(statements) != 1:
        return ValidationResult(False, sql, "Exactly one SQL statement is allowed")
    tree = statements[0]

    if not isinstance(tree, exp.Select):  # UPDATE, DELETE, INSERT, DROP, EXEC ... are other node types
        return ValidationResult(False, sql, f"Only SELECT is allowed (got {type(tree).__name__})")
    if tree.find(exp.Into):
        return ValidationResult(False, sql, "SELECT ... INTO is not allowed")
    for node in tree.find_all(exp.Anonymous, exp.Func):
        if node.name.lower() in FORBIDDEN_FUNCTIONS:
            return ValidationResult(False, sql, f"Function {node.name} is not allowed")
    # Data-changing statements hidden inside the tree (should not parse as SELECT, but double-check)
    for bad in (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command):
        if tree.find(bad):
            return ValidationResult(False, sql, f"{bad.__name__} is not allowed")

    # Tables (CTE names are not real tables)
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    tables = {t.name.lower() for t in tree.find_all(exp.Table)} - cte_names
    if not tables:
        return ValidationResult(False, sql, "Query must read from a table")
    for t in tables:
        if t in NEVER_ALLOWED:
            return ValidationResult(False, sql, f"Table {t} must never be queried (no index)")
        if t not in allowed_tables:
            return ValidationResult(False, sql, f"Table {t} is not in the allowed list")

    # Large tables need a date condition in a WHERE clause of the same SELECT
    # (each SELECT - including nested ones - is checked against the tables DIRECTLY in its own FROM)
    for select in tree.find_all(exp.Select):
        from_clause = select.args.get("from_") or select.args.get("from")  # key name differs between sqlglot versions
        direct = from_clause.this if from_clause else None
        for table in [direct] if isinstance(direct, exp.Table) else []:
            date_col = LARGE_TABLE_DATE_COLUMNS.get(table.name.lower())
            if not date_col:
                continue
            where = select.args.get("where")
            where_cols = {c.name.lower() for c in where.find_all(exp.Column)} if where else set()
            if date_col not in where_cols:
                return ValidationResult(False, sql, f"Table {table.name} needs a filter on {date_col} in WHERE")
        for join in select.args.get("joins") or []:
            date_col = LARGE_TABLE_DATE_COLUMNS.get(join.this.name.lower()) if isinstance(join.this, exp.Table) else None
            if date_col:
                where = select.args.get("where")
                cols = {c.name.lower() for c in where.find_all(exp.Column)} if where else set()
                cols |= {c.name.lower() for c in join.find_all(exp.Column)}
                if date_col not in cols:
                    return ValidationResult(False, sql, f"Joined table {join.this.name} needs a filter on {date_col}")

    # Row limit on the outer query
    limit = tree.args.get("limit")
    if limit is None:
        if not add_top:
            return ValidationResult(False, sql, "A TOP n row limit is required")
        tree = tree.limit(max_rows)
        sql = tree.sql(dialect="tsql")
    else:
        try:
            n = int(limit.expression.name)
        except (AttributeError, ValueError):
            return ValidationResult(False, sql, "TOP must be a number")
        if n > max_rows:
            return ValidationResult(False, sql, f"TOP {n} is larger than the maximum {max_rows}")
    return ValidationResult(True, sql)
