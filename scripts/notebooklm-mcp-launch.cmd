@echo off
REM ============================================================
REM  NotebookLM MCP launcher with auto auth-refresh
REM  - Runs `nlm login` first to refresh tokens from the local
REM    default profile (silent if creds valid; opens browser
REM    ONLY when auth has truly expired).
REM  - All nlm output is redirected to a log file so it never
REM    corrupts the MCP stdout (JSON-RPC) stream.
REM  - Then execs the real MCP server, inheriting stdio.
REM ============================================================

setlocal

set "NLM_DIR=%USERPROFILE%\.notebooklm-mcp-cli"
set "LOG=%NLM_DIR%\launch-auth.log"
if not exist "%NLM_DIR%" mkdir "%NLM_DIR%" 2>nul

REM --- pin the storage/config dir so the MCP server reads the SAME ----
REM     profile (cookies, csrf, chrome-profiles) that the nlm CLI writes.
REM     The server resolves all of these from NOTEBOOKLM_MCP_CLI_PATH and
REM     falls back to Path.home(), which can differ from the CLI's HOME
REM     when launched by an MCP host. Pinning it removes that ambiguity.
set "NOTEBOOKLM_MCP_CLI_PATH=%NLM_DIR%"

REM --- locate the venv that holds nlm.exe / notebooklm-mcp.exe ----
REM     ADJUST THIS PATH to where your virtual environment lives.
REM     Example assumes the venv is at <this-script-dir>\notebooklm-mcp\.venv
set "VENV_SCRIPTS=%~dp0notebooklm-mcp\.venv\Scripts"

REM --- locate nlm.exe -------------------------------------------
REM Prefer the venv copy, then PATH, then the legacy ~/.local/bin.
set "NLM="
if exist "%VENV_SCRIPTS%\nlm.exe" set "NLM=%VENV_SCRIPTS%\nlm.exe"
if not defined NLM where nlm >nul 2>&1 && set "NLM=nlm"
if not defined NLM if exist "%USERPROFILE%\.local\bin\nlm.exe" set "NLM=%USERPROFILE%\.local\bin\nlm.exe"

REM --- locate the MCP server entrypoint -------------------------
set "MCP="
if exist "%VENV_SCRIPTS%\notebooklm-mcp.exe" set "MCP=%VENV_SCRIPTS%\notebooklm-mcp.exe"
if not defined MCP where notebooklm-mcp >nul 2>&1 && set "MCP=notebooklm-mcp"
if not defined MCP if exist "%USERPROFILE%\.local\bin\notebooklm-mcp.exe" set "MCP=%USERPROFILE%\.local\bin\notebooklm-mcp.exe"

echo [%date% %time%] launch: refreshing auth (NLM=%NLM%) >> "%LOG%"

REM Refresh auth. NOTE: stdout AND stderr go to the log so the
REM MCP protocol stream stays clean.
if defined NLM (
    call "%NLM%" login >> "%LOG%" 2>&1
    echo [%date% %time%] nlm login exit=%errorlevel% >> "%LOG%"
) else (
    echo [%date% %time%] WARNING: nlm.exe not found, skipping auth refresh >> "%LOG%"
)

REM --- start the MCP server (stdio passes through) ----------
REM If your config used different args, append them after %MCP%.
"%MCP%" %*
