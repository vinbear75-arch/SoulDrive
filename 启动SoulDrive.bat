@echo off
setlocal

set "ROOT=%~dp0"
for %%I in ("%ROOT%souldrive-ui\src-tauri\target\debug\souldrive-ui.exe") do set "SOULDRIVE_EXE=%%~fI"
powershell -NoProfile -Command "$exe=$env:SOULDRIVE_EXE; if (Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'souldrive-ui.exe' -and $_.ExecutablePath -eq $exe }) { exit 0 }; exit 1"
if not errorlevel 1 (
  echo SoulDrive is already running. Reusing the existing window.
  exit /b 0
)
set "SOULDRIVE_WORKSPACE_MODE=local"
set "SOULDRIVE_DEMO_WORKSPACE=1"
set "SOULDRIVE_WORKSPACE=%ROOT%runtime\workspace"
set "SOULDRIVE_PC_ONLY=1"
set "SOULDRIVE_DEV_BYPASS_WORKSPACE_LOCK=1"
set "SOULDRIVE_DEV_WORKSPACE_PASSPHRASE=88888888"
set "SOULDRIVE_CHAT_PROVIDER=ollama"
set "SOULDRIVE_OLLAMA_MODEL=qwen3.5:9b"
set "OLLAMA_MODELS=D:\SOFTWARES\Ollama\models"
set "OLLAMA_HOST=127.0.0.1:11434"
set "SOULDRIVE_EMBEDDING_PROVIDER=ollama"
set "SOULDRIVE_OLLAMA_EMBEDDING_MODEL=qwen3-embedding:4b"
set "SOULDRIVE_REDIS_ENABLED=1"
set "SOULDRIVE_REDIS_URL=redis://127.0.0.1:6379/0"
set "SOULDRIVE_REDIS_MAX_MESSAGES=32"
set "SOULDRIVE_REDIS_TTL_SECONDS=604800"
set "SOULDRIVE_POSTGRES_ENABLED=1"
set "SOULDRIVE_POSTGRES_URL=postgresql://souldrive:souldrive@127.0.0.1:5432/souldrive"
set "SOULDRIVE_SESSION_COMPACTION_THRESHOLD=16"
set "SOULDRIVE_RATE_LIMIT_PER_MINUTE=600"
set "SOULDRIVE_MAX_BODY_BYTES=16777216"

set "SOULDRIVE_REDIS_HOME=D:\SOFTWARES\RedisVector\redis-8.10.0\Redis-8.10.0-Windows-x64-msys2"
set "SOULDRIVE_REDIS_DATA=D:\SOFTWARES\RedisVector\data"
if exist "%SOULDRIVE_REDIS_HOME%\redis-cli.exe" (
  if not exist "%SOULDRIVE_REDIS_DATA%" mkdir "%SOULDRIVE_REDIS_DATA%"
  "%SOULDRIVE_REDIS_HOME%\redis-cli.exe" -h 127.0.0.1 -p 6379 ping >nul 2>&1
  if errorlevel 1 start "" /min /b "%SOULDRIVE_REDIS_HOME%\redis-server.exe" --bind 127.0.0.1 --port 6379 --appendonly yes --dir "%SOULDRIVE_REDIS_DATA%"
)

set "SOULDRIVE_POSTGRES_HOME=D:\SOFTWARES\PostgreSQL\17"
set "SOULDRIVE_POSTGRES_DATA=D:\SOFTWARES\PostgreSQL\data"
if exist "%SOULDRIVE_POSTGRES_HOME%\bin\pg_ctl.exe" (
  "%SOULDRIVE_POSTGRES_HOME%\bin\pg_ctl.exe" status -D "%SOULDRIVE_POSTGRES_DATA%" >nul 2>&1
  if errorlevel 1 "%SOULDRIVE_POSTGRES_HOME%\bin\pg_ctl.exe" start -D "%SOULDRIVE_POSTGRES_DATA%" -l "%SOULDRIVE_POSTGRES_DATA%\server.log" >nul 2>&1
)

where ollama.exe >nul 2>&1
if not errorlevel 1 (
  powershell -NoProfile -Command "$ready=Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction SilentlyContinue; if (-not $ready) { exit 1 }"
  if errorlevel 1 start "" /min /b ollama.exe serve
)

cd /d "%ROOT%souldrive-ui"
powershell -NoProfile -Command "$root='%ROOT%'; Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -like ('*'+$root+'souldrive-ui*vite*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
call npm.cmd run tauri dev

endlocal
