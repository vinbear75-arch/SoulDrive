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
set "SOULDRIVE_WORKSPACE=%ROOT%runtime\workspace"
set "SOULDRIVE_PC_ONLY=1"
if not defined SOULDRIVE_CHAT_PROVIDER set "SOULDRIVE_CHAT_PROVIDER=ollama"
if not defined SOULDRIVE_OLLAMA_MODEL set "SOULDRIVE_OLLAMA_MODEL=qwen3.5:9b"
if not defined OLLAMA_HOST set "OLLAMA_HOST=127.0.0.1:11434"
if not defined SOULDRIVE_EMBEDDING_PROVIDER set "SOULDRIVE_EMBEDDING_PROVIDER=ollama"
if not defined SOULDRIVE_OLLAMA_EMBEDDING_MODEL set "SOULDRIVE_OLLAMA_EMBEDDING_MODEL=qwen3-embedding:4b"
if not defined SOULDRIVE_REDIS_ENABLED set "SOULDRIVE_REDIS_ENABLED=0"
if not defined SOULDRIVE_POSTGRES_ENABLED set "SOULDRIVE_POSTGRES_ENABLED=0"

where ollama.exe >nul 2>&1
if not errorlevel 1 (
  powershell -NoProfile -Command "$ready=Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction SilentlyContinue; if (-not $ready) { exit 1 }"
  if errorlevel 1 start "" /min /b ollama.exe serve
)

cd /d "%ROOT%souldrive-ui"
call npm.cmd run tauri dev

endlocal
