@echo off
title Crumble Hangout
cd /d "%~dp0"
echo Installing / checking aiohttp...
python -m pip install -q -r requirements.txt
if exist "C:\cloudflared\cloudflared.exe" (
  echo Starting a public cloudflared link - look for the trycloudflare.com URL in the new window.
  start "cloudflared" "C:\cloudflared\cloudflared.exe" tunnel --url http://localhost:8080
)
echo Game: http://localhost:8080
python server.py
pause
