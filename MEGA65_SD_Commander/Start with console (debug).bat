@echo off
rem Start MEGA65 SD Commander with a console window, to see any error messages.
cd /d "%~dp0"
where python >nul 2>nul && (python m65_sd_commander.py & goto done)
where py >nul 2>nul && (py -3 m65_sd_commander.py & goto done)
echo Python 3 was not found. See INSTALL.md.
:done
echo.
echo Program closed. Press a key to close this window.
pause >nul
