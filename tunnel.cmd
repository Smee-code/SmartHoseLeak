@echo off
REM ---------------------------------------------------------------------
REM  Open this system to the internet over HTTPS, for somebody who is not
REM  on the same WiFi.
REM
REM  Run it in a second window, next to the one already running
REM      python manage.py runserver 0.0.0.0:8000
REM
REM  It prints a https://....trycloudflare.com address. Send that to
REM  whoever needs it. The address changes every time this is restarted;
REM  nothing needs editing when it does, because settings.py already
REM  trusts *.trycloudflare.com for CSRF.
REM
REM  Closing this window closes the door.
REM
REM  Why the full path: the cloudflared installer puts the folder on the
REM  system PATH, but a Command Prompt that was already open keeps the PATH
REM  it started with - so "cloudflared" is not recognised in the window you
REM  were already using. Naming the exe outright sidesteps that entirely.
REM
REM  Why 127.0.0.1 and not localhost: on this laptop "localhost" resolves to
REM  the IPv6 address ::1 first, and the development server listens on IPv4
REM  only, so "localhost" can fail while 127.0.0.1 always works.
REM ---------------------------------------------------------------------

setlocal

set "CF=C:\Program Files (x86)\cloudflared\cloudflared.exe"
if not exist "%CF%" set "CF=C:\Program Files\cloudflared\cloudflared.exe"

if not exist "%CF%" (
  echo.
  echo cloudflared is not installed. Install it once with:
  echo.
  echo     winget install --id Cloudflare.cloudflared
  echo.
  pause
  exit /b 1
)

echo.
echo Checking the server is actually running on port 8000...
curl -s -o nul --max-time 5 http://127.0.0.1:8000/
if errorlevel 1 (
  echo.
  echo   Nothing is answering on http://127.0.0.1:8000
  echo.
  echo   Start the server first, in another window:
  echo       python manage.py runserver 0.0.0.0:8000
  echo.
  echo   Starting the tunnel anyway - it will work once the server is up.
  echo.
) else (
  echo   Server is up.
  echo.
)

echo Opening the tunnel. The https://....trycloudflare.com address below is
echo the one to send. Press Ctrl+C to close it.
echo.

"%CF%" tunnel --url http://127.0.0.1:8000 --no-autoupdate

endlocal
