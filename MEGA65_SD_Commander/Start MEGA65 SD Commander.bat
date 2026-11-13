@echo off
rem Start MEGA65 SD Commander without a console window.
where pythonw >nul 2>nul && (start "" pythonw "%~dp0m65_sd_commander.py" & exit /b)
where pyw >nul 2>nul && (start "" pyw "%~dp0m65_sd_commander.py" & exit /b)
echo Python 3 was not found.
echo Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),
echo then run this again. See INSTALL.md for details.
pause
