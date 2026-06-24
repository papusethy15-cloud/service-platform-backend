# Run DB migration from PowerShell
# Usage: .\run_migration.ps1

$env:PGPASSWORD = "palei_pass"
psql -h localhost -p 5433 -U palei_user -d palei_solutions -f "$PSScriptRoot\migrate_commission_parts.sql"
