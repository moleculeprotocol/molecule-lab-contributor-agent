@echo off
rem Starts the mol-labs MCP server on Windows. .mcp.json runs this through cmd.exe.
rem
rem Same job as mcp/launch: make the very first spawn succeed. If the SessionStart hook is
rem setting up (it runs under Git Bash), wait for it; if it never ran, install the private uv
rem here with the official PowerShell installer; then hand over to uv. Nothing is written to
rem stdout - that is the MCP channel.
setlocal
set "ROOT=%CLAUDE_PLUGIN_ROOT%"
set "DATA=%CLAUDE_PLUGIN_DATA%"
rem Opened as a plain project rather than a plugin: this checkout, and a gitignored folder in it.
if "%ROOT%"=="" set "ROOT=%~dp0.."
if "%DATA%"=="" set "DATA=%ROOT%\.plugin-data"
set "UV=%DATA%\uv\uv.exe"
set "LOCK=%DATA%\bootstrap.lock"
set "LOG=%DATA%\bootstrap.log"

set /a waited=0
:wait
if exist "%LOCK%" (
  if %waited% geq 600 goto :after_wait
  set /a waited+=1
  ping -n 2 127.0.0.1 >nul
  goto :wait
)
:after_wait

if exist "%UV%" goto :run
mkdir "%DATA%\uv" 2>nul
mkdir "%LOCK%" 2>nul
echo === %DATE% %TIME% launch.cmd installing uv >> "%LOG%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$env:UV_INSTALL_DIR='%DATA%\uv'; $env:UV_NO_MODIFY_PATH='1'; irm https://astral.sh/uv/install.ps1 | iex" >> "%LOG%" 2>&1
rmdir "%LOCK%" 2>nul
if not exist "%UV%" (
  echo mol-labs: could not install uv - see %LOG% 1>&2
  exit /b 1
)

:run
"%UV%" run --script "%ROOT%\mcp\server.py"
