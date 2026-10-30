$pw = Read-Host "Password"
$conn = New-Object System.Data.SqlClient.SqlConnection "Server=10.4.8.44;User ID=AI;Password=$pw"
try { $conn.Open(); "Connected." } catch { "Connection failed: $($_.Exception.Message)"; exit }

$cmd = $conn.CreateCommand()
$cmd.CommandText = "USE DB_PlantLive;"
[void]$cmd.ExecuteNonQuery()
$exclude = 'RowError','RowState','Table','ItemArray','HasErrors'
$desktop = "$env:USERPROFILE\Desktop"

# 1. Tables: row counts, column counts, date columns
$cmd.CommandText = @"
SELECT s.name AS schema_name, t.name AS table_name, SUM(p.rows) AS row_count,
  (SELECT COUNT(*) FROM sys.columns c WHERE c.object_id = t.object_id) AS column_count,
  STUFF((SELECT ', ' + c.name FROM sys.columns c
         JOIN sys.types ty ON c.user_type_id = ty.user_type_id
         WHERE c.object_id = t.object_id
           AND ty.name IN ('date','datetime','datetime2','smalldatetime','datetimeoffset')
         FOR XML PATH('')), 1, 2, '') AS date_columns
FROM sys.tables t
JOIN sys.schemas s ON t.schema_id = s.schema_id
JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0,1)
GROUP BY s.name, t.name, t.object_id
ORDER BY row_count DESC;
"@
$dt = New-Object System.Data.DataTable
[void](New-Object System.Data.SqlClient.SqlDataAdapter $cmd).Fill($dt)
$dt | Format-Table -AutoSize
$dt | Select-Object * -ExcludeProperty $exclude | Export-Csv "$desktop\plantlive_tables.csv" -NoTypeInformation

# 2. All columns with data types
$cmd.CommandText = "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS ORDER BY TABLE_NAME, ORDINAL_POSITION"
$dt2 = New-Object System.Data.DataTable
[void](New-Object System.Data.SqlClient.SqlDataAdapter $cmd).Fill($dt2)
$dt2 | Select-Object * -ExcludeProperty $exclude | Export-Csv "$desktop\plantlive_columns.csv" -NoTypeInformation

$conn.Close()
"Done. Saved plantlive_tables.csv and plantlive_columns.csv to Desktop."