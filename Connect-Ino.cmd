@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0services\inoproshop-mcp\scripts\Connect-Ino.ps1" %*
exit /b %ERRORLEVEL%
