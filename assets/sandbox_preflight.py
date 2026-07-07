"""Pre-flight checks before enabling sandbox mode (Phase 10).

Sandbox mode REQUIRES three things, in order:
  1. WSL installed (`wsl --status` succeeds)
  2. Docker Engine callable inside WSL without sudo (`wsl bash -c 'docker info'`)
  3. The `agent-sandbox` container exists and is running

If anything fails, the user is told exactly what to do — usually run
`sandbox\\setup_sandbox.bat` from a regular PowerShell/cmd window. We do NOT
silently fall back to host mode; that hides config drift.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

CONTAINER_NAME = "agent-sandbox"


@dataclass
class PreflightResult:
    ok: bool
    container: str
    failures: list[str]
    hints: list[str]


def preflight_sandbox(container: str = CONTAINER_NAME) -> PreflightResult:
    failures: list[str] = []
    hints: list[str] = []

    # 1. WSL command available on host?
    if not shutil.which("wsl"):
        failures.append("WSL not installed on host.")
        hints.append("Admin PowerShell:  wsl --install   (reboot afterward)")
        return PreflightResult(False, container, failures, hints)

    # 2. WSL has a usable distro & docker is callable (no sudo)?
    r = subprocess.run(
        ["wsl", "-e", "bash", "-c", "docker info >/dev/null 2>&1 && echo OK || echo FAIL"],
        capture_output=True, text=True, timeout=15,
    )
    if "OK" not in (r.stdout or ""):
        failures.append("Docker Engine in WSL not callable without sudo.")
        hints.append("Run:  sandbox\\setup_sandbox.bat")
        hints.append("If it just installed Docker, run:  wsl --shutdown")
        return PreflightResult(False, container, failures, hints)

    # 3. Container exists and is running?
    r2 = subprocess.run(
        ["wsl", "-e", "bash", "-c",
         f"docker ps --format '{{{{.Names}}}}' | grep -q '^{container}$' && echo YES || echo NO"],
        capture_output=True, text=True, timeout=15,
    )
    if "YES" not in (r2.stdout or ""):
        # Maybe the container exists but isn't running — try to start it
        r3 = subprocess.run(
            ["wsl", "-e", "bash", "-c",
             f"docker ps -a --format '{{{{.Names}}}}' | grep -q '^{container}$' && echo EXISTS || echo MISSING"],
            capture_output=True, text=True, timeout=15,
        )
        if "EXISTS" in (r3.stdout or ""):
            subprocess.run(
                ["wsl", "-e", "bash", "-c", f"docker start {container}"],
                capture_output=True, text=True, timeout=20,
            )
            # Re-check
            r4 = subprocess.run(
                ["wsl", "-e", "bash", "-c",
                 f"docker ps --format '{{{{.Names}}}}' | grep -q '^{container}$' && echo YES || echo NO"],
                capture_output=True, text=True, timeout=15,
            )
            if "YES" in (r4.stdout or ""):
                return PreflightResult(True, container, [], ["Started existing stopped container."])
        failures.append(f"Container `{container}` not running (or missing).")
        hints.append("Run:  sandbox\\setup_sandbox.bat")
        return PreflightResult(False, container, failures, hints)

    return PreflightResult(True, container, [], [])


if __name__ == "__main__":
    import sys
    res = preflight_sandbox()
    if res.ok:
        print(f"[OK] sandbox ready (container={res.container})")
        if res.hints:
            for h in res.hints:
                print(f"     {h}")
        sys.exit(0)
    print("[FAIL] sandbox pre-flight:")
    for f in res.failures:
        print(f"  - {f}")
    print("\nTo fix:")
    for h in res.hints:
        print(f"  - {h}")
    sys.exit(1)
