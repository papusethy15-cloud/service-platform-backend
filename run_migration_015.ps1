# Run migration 015 — add GST fields to customers
# Run this from the backend directory in PowerShell/cmd

Write-Host "Running migration 015: Add GST fields to customers table..." -ForegroundColor Cyan

Set-Location $PSScriptRoot

# Activate venv
& ".\venv\Scripts\Activate.ps1"

# Run alembic upgrade
alembic upgrade 015_customer_gst_fields

if ($LASTEXITCODE -eq 0) {
    Write-Host "✅ Migration applied successfully!" -ForegroundColor Green
    Write-Host "Now restart the backend server (Ctrl+C then re-run uvicorn)." -ForegroundColor Yellow
} else {
    Write-Host "❌ Migration failed. Check output above." -ForegroundColor Red
}
