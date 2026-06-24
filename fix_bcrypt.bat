@echo off
echo Fixing bcrypt version incompatibility with passlib 1.7.4 ...
call "C:\MyWorkspace\Palei Solutions\backend\venv\Scripts\activate.bat"
pip install "bcrypt==4.0.1" --force-reinstall
echo.
echo Done. Now run: uvicorn app.main:app --reload
pause
