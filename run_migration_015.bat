@echo off
echo Running migration 015: Add GST fields to customers table...
cd /d "%~dp0"
call venv\Scripts\activate.bat
alembic upgrade 015_customer_gst_fields
if %ERRORLEVEL% EQU 0 (
    echo.
    echo SUCCESS - Migration applied!
    echo Please restart your backend server.
) else (
    echo.
    echo FAILED - Check error above.
)
pause
