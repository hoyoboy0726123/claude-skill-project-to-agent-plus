@echo off
REM ============================================================
REM  *** DO NOT REWRITE THIS .BAT FROM SCRATCH ***
REM
REM  This file has 3 fixes for known traps. If you "simplify" it
REM  you WILL hit all three (battle-tested across multiple agents
REM  that tried to roll their own):
REM
REM    FIX 1   chcp 65001 >NUL        UTF-8 codepage; else CJK
REM                                   echo = mojibake
REM    FIX 2   wslpath -a "%~dp0.."   translate to /mnt/c/...;
REM                                   else WSL cwd=~ and
REM                                   "./sandbox/setup.sh: No
REM                                   such file or directory"
REM    FIX 3   sed -i 's/\r$//'       strip CRLF from setup.sh;
REM                                   else "\r: command not
REM                                   found" / "invalid option:
REM                                   pipefail\r"
REM
REM  Want CJK output? Keep chcp 65001 and write strings in UTF-8.
REM  Prefer zero risk? Stick with the all-English strings below.
REM ============================================================
REM
REM Switch to UTF-8 so CJK echo / pause strings parse correctly on Traditional
REM Chinese Windows (default CP950 misreads UTF-8 multibyte → 'xxx is not recognized' errors).
chcp 65001 >NUL

REM ============================================================
REM project-to-agent — Sandbox one-click installer (Windows entry)
REM
REM What this does:
REM   1. Verify WSL is installed (otherwise tell user to run wsl --install)
REM   2. Verify a usable WSL distro exists (Ubuntu etc.)
REM   3. Translate this project path to WSL form (/mnt/c/...)
REM   4. Self-heal CRLF on setup.sh (Windows git autocrlf strips LF endings)
REM   5. Call sandbox/setup.sh inside WSL to do the actual install
REM
REM Why custom .bat (instead of Docker Desktop):
REM   Docker Desktop has commercial licensing restrictions; we install
REM   Docker Engine from get.docker.com directly inside WSL — free for
REM   any use, including commercial.
REM
REM Usage:
REM   setup_sandbox.bat              normal install (first time)
REM   setup_sandbox.bat --rebuild    force rebuild image (after editing Dockerfile)
REM ============================================================

SETLOCAL
SET "EXTRA_ARGS=%*"

echo ==================================================
echo  project-to-agent - Sandbox Setup
echo ==================================================
echo.

REM 1. WSL installed?
wsl --status >NUL 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [X] WSL not detected.
    echo.
    echo Run in an ADMIN PowerShell:
    echo.
    echo     wsl --install
    echo.
    echo Then reboot and rerun this script.
    echo.
    pause
    exit /b 1
)

REM 2. A usable WSL distro exists?
wsl -e bash -c "echo OK" >NUL 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [X] No usable WSL distro.
    echo Install Ubuntu via:  wsl --install -d Ubuntu
    pause
    exit /b 1
)

REM 3. Translate this project dir to WSL path
SET "WIN_PROJECT=%~dp0.."
FOR /F "usebackq tokens=*" %%F IN (`wsl wslpath -a "%WIN_PROJECT%"`) DO SET "WSL_PROJECT=%%F"

echo Windows project : %WIN_PROJECT%
echo WSL project     : %WSL_PROJECT%
echo.

REM 4. CRLF self-heal (Windows git core.autocrlf=true rewrites .sh to CRLF,
REM    which breaks WSL bash: 'set -euo pipefail\r' = invalid option).
wsl -e bash -c "sed -i 's/\r$//' '%WSL_PROJECT%/sandbox/setup.sh'" >NUL 2>&1

REM 5. Run the WSL-side setup
echo === Running setup inside WSL ===
echo (First-time install may prompt for your WSL sudo password.)
echo.
wsl bash "%WSL_PROJECT%/sandbox/setup.sh" "%WSL_PROJECT%" %EXTRA_ARGS%

IF %ERRORLEVEL% NEQ 0 (
    echo.
    echo !! Setup FAILED. See messages above.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo ==================================================
echo  Sandbox is ready.
echo ==================================================
echo.

REM Detect: WSL needs a restart. Reasons:
REM   (a) Docker just installed, docker group membership not yet active
REM   (b) /etc/wsl.conf systemd=true just enabled, not yet active
REM Either case: wsl --shutdown lets the changes load fresh.
REM Without this, host reboot would leave dockerd stopped and every shell
REM command would prompt for sudo or fail.
IF EXIST "%~dp0.needs_wsl_shutdown" (
    echo ==================================================
    echo  [!] IMPORTANT: WSL needs a restart
    echo ==================================================
    echo.
    echo  Either Docker was just installed (group not active) or systemd
    echo  was just enabled in /etc/wsl.conf. Both require WSL restart.
    echo  Without restart, dockerd will not auto-start after Windows reboot
    echo  and every container command may prompt for sudo.
    echo.
    SET /P SHUTDOWN_ANS="Run 'wsl --shutdown' now? (Y/N, default Y): "
    IF /I NOT "%SHUTDOWN_ANS%"=="N" (
        echo.
        echo ==^> wsl --shutdown ...
        wsl --shutdown
        echo (V) WSL stopped. Next WSL command will load systemd + docker group.
    ) ELSE (
        echo.
        echo [i] Remember to run manually:  wsl --shutdown
    )
    del "%~dp0.needs_wsl_shutdown" >NUL 2>&1
    echo.
)

echo Next: in your agent settings set sandbox_mode = true.
echo       The agent will route shell commands through the container.
echo.
pause
