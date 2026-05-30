@echo off
cd /d "%~dp0"
echo Installing required packages...
pip install -r requirements.txt
echo.
echo Running Epic Games setup...
echo When it finds your password and asks for a 2FA code,
echo open your authenticator app and type the 6 digits shown RIGHT THEN.
echo.
python setup_epic.py
echo.
pause
