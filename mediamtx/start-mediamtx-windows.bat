@echo off
REM Lance MediaMTX en NATIF Windows (PAS Docker).
REM Utilise mediamtx.windows.yml -> auth sur localhost:8000 (pas host.docker.internal).
cd /d "%~dp0"
echo Demarrage MediaMTX (Windows natif)...
mediamtx.exe mediamtx.windows.yml
