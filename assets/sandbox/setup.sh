#!/usr/bin/env bash
# project-to-agent — Sandbox install inside WSL Ubuntu
#
# Called from Windows by setup_sandbox.bat. Does:
#   1. If Docker Engine not installed → install via get.docker.com
#      (we deliberately DO NOT use Docker Desktop because of its commercial
#       licensing terms; Docker Engine is free for any use.)
#   2. Build the agent sandbox image (if not already built)
#   3. Start a long-running container `agent-sandbox` with the project bind-mounted
#
# Afterwards the agent will exec into the container via:
#   wsl docker exec agent-sandbox python -c "..."
#
# Usage:
#   setup.sh <project_dir_in_wsl>              first install / no-op rebuild
#   setup.sh <project_dir_in_wsl> --rebuild    force rebuild image + container
#                                              (use after editing Dockerfile)
set -euo pipefail

PROJECT_DIR="${1:-}"
REBUILD="no"
for arg in "${@:2}"; do
    case "$arg" in
        --rebuild|-r) REBUILD="yes" ;;
    esac
done
if [[ -z "$PROJECT_DIR" ]]; then
    echo "Usage: $0 <project_dir_in_wsl> [--rebuild]"
    exit 1
fi
if [[ ! -d "$PROJECT_DIR" ]]; then
    echo "✗ Project dir not found: $PROJECT_DIR"
    exit 1
fi

CONTAINER="agent-sandbox"
IMAGE="agent-sandbox:latest"

echo "══════════════════════════════════════════════════════"
echo "project-to-agent — Sandbox install"
echo "══════════════════════════════════════════════════════"
echo "Project: $PROJECT_DIR"
echo ""

# ── 1. Docker CLI prefix detection ──────────────────────────────
# Prefer plain `docker`; only use sudo if user not yet in docker group.
# Adding user to docker group during install requires WSL restart to take effect.
if docker info &>/dev/null; then
    DOCKER="docker"
    echo "✓ docker callable without sudo"
else
    DOCKER="sudo docker"
    echo "ℹ docker requires sudo (user not in docker group, or WSL not restarted yet)"
fi
echo ""

# ── 2. Install Docker Engine if missing ─────────────────────────
if ! command -v docker &>/dev/null; then
    echo "==> Docker not installed — running official installer (curl get.docker.com, ~2-3 min)..."
    echo "    (We use Docker Engine, not Docker Desktop — Docker Desktop has commercial"
    echo "     license restrictions; Docker Engine is free for all use cases.)"
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER"
    echo "✓ Docker Engine installed"
    echo "  ⚠ Added current user to docker group — after this script ends, the BAT"
    echo "    will prompt 'wsl --shutdown' so group membership refreshes."
else
    echo "✓ Docker present: $(docker --version)"
fi

# ── 3. Enable WSL systemd so dockerd auto-starts on every WSL boot ─
# Without systemd, dockerd dies when WSL sleeps + needs manual `service docker start`
# after every Windows reboot. With systemd, dockerd auto-starts with WSL.
# Idempotent: only writes /etc/wsl.conf if systemd isn't enabled.
NEED_WSL_SHUTDOWN_FOR_SYSTEMD=no
if ! systemctl --version &>/dev/null 2>&1; then
    echo "==> Enabling systemd in /etc/wsl.conf (needed for dockerd auto-start on reboot)..."
    if [[ -f /etc/wsl.conf ]] && grep -q "^systemd=true" /etc/wsl.conf; then
        echo "  (already set, but not active — needs wsl --shutdown)"
    else
        sudo bash -c 'if [[ -f /etc/wsl.conf ]]; then
            if grep -q "^\[boot\]" /etc/wsl.conf; then
                sed -i "/^\[boot\]/a systemd=true" /etc/wsl.conf
            else
                echo -e "\n[boot]\nsystemd=true" >> /etc/wsl.conf
            fi
        else
            echo -e "[boot]\nsystemd=true" > /etc/wsl.conf
        fi'
        echo "  ✓ Wrote systemd=true to /etc/wsl.conf"
    fi
    NEED_WSL_SHUTDOWN_FOR_SYSTEMD=yes
fi

# Start dockerd if not running yet (works for systemd + non-systemd cases)
if [[ "$DOCKER" == "sudo docker" ]] && ! sudo -n service docker status &>/dev/null; then
    echo "==> Starting docker daemon..."
    sudo service docker start
fi

# ── 4. Build the sandbox image ──────────────────────────────────
if [[ "$REBUILD" == "yes" ]]; then
    echo "==> --rebuild: removing existing container + image..."
    $DOCKER rm -f "$CONTAINER" 2>/dev/null || true
    $DOCKER rmi -f "$IMAGE" 2>/dev/null || true
    echo "==> Rebuilding image $IMAGE (no cache)..."
    $DOCKER build --no-cache -t "$IMAGE" "$PROJECT_DIR/sandbox"
    echo "✓ Image rebuilt"
elif [[ "$($DOCKER images -q $IMAGE 2>/dev/null)" == "" ]]; then
    echo "==> Building sandbox image $IMAGE (first time, ~2-5 min)..."
    $DOCKER build -t "$IMAGE" "$PROJECT_DIR/sandbox"
    echo "✓ Image built"
else
    echo "✓ Image exists: $IMAGE"
    echo "  (edit Dockerfile / requirements.txt then re-run with --rebuild to update)"
fi

# ── 5. Create / start the long-running container ────────────────
# Bind-mount the project path with the SAME path in container, so that any
# code the agent generates that uses absolute paths (Path.resolve() etc.)
# resolves to the same location both inside and outside the container.
if $DOCKER ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    if $DOCKER ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
        echo "✓ Container $CONTAINER already running"
    else
        echo "==> Starting existing container $CONTAINER..."
        $DOCKER start "$CONTAINER"
    fi
else
    echo "==> Creating and starting container $CONTAINER..."
    $DOCKER run -d \
        --name "$CONTAINER" \
        --restart unless-stopped \
        -v "$PROJECT_DIR:$PROJECT_DIR" \
        -w "$PROJECT_DIR" \
        "$IMAGE" \
        tail -f /dev/null
    echo "✓ Container running with bind-mount:"
    echo "    $PROJECT_DIR → $PROJECT_DIR"
fi

# ── 6. Smoke test ───────────────────────────────────────────────
echo ""
echo "==> Smoke test — core packages:"
if ! $DOCKER exec "$CONTAINER" python -c "import sys; print(f'  ✓ Python {sys.version.split()[0]}')"; then
    echo "✗ Smoke test failed"
    exit 1
fi

echo ""
echo "══════════════════════════════════════════════════════"
echo "✓ Sandbox ready."
echo "  Container : $CONTAINER"
echo "  Image     : $IMAGE"
echo "══════════════════════════════════════════════════════"

# ── 7. Flag for the .bat to detect: was docker freshly installed OR systemd just enabled? ──
# Either case requires `wsl --shutdown` to take effect:
#   - docker group membership refresh (otherwise commands need sudo)
#   - /etc/wsl.conf systemd=true activation (otherwise dockerd doesn't auto-start)
FLAG_FILE="$PROJECT_DIR/sandbox/.needs_wsl_shutdown"
if [[ "$DOCKER" == "sudo docker" ]] || [[ "$NEED_WSL_SHUTDOWN_FOR_SYSTEMD" == "yes" ]]; then
    touch "$FLAG_FILE" 2>/dev/null || true
else
    rm -f "$FLAG_FILE" 2>/dev/null || true
fi
