# project-to-agent — Minimal sandbox image
#
# Lightweight Python 3.13 base + CJK locale + a few common packages.
# Add more layers below for your specific project deps; keep them as
# separate RUN steps so the WSL2 builder doesn't OOM on big single-layer
# installs (default WSL2 memory is 8GB).
#
# Build:   docker build -t agent-sandbox:latest .
# Don't:   include heavy GUI stack (pyautogui / mss / pynput etc.) —
#          containers have no display, those belong on host.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# ── System: locale + curl + ca-certs ────────────────────────────
# CJK locale lets stdout print Traditional Chinese without mojibake.
# Most agent tools log human-readable text → worth the ~20MB cost.
RUN apt-get update && apt-get install -y --no-install-recommends \
        locales ca-certificates curl \
    && sed -i '/zh_TW.UTF-8/s/^# //g; /en_US.UTF-8/s/^# //g' /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/*
ENV LANG=zh_TW.UTF-8 LC_ALL=zh_TW.UTF-8

# ── Tier 1: data manipulation + I/O ─────────────────────────────
RUN pip install --no-cache-dir \
    pandas openpyxl numpy python-dateutil pytz Pillow

# ── Tier 2: HTTP / parsing ──────────────────────────────────────
RUN pip install --no-cache-dir \
    requests beautifulsoup4 lxml "httpx[http2]"

# ── (Add your project-specific layers below, one RUN per group) ──
# Example:
# RUN pip install --no-cache-dir matplotlib seaborn
# RUN pip install --no-cache-dir python-docx pypdfium2

# Default command keeps the container alive so the agent can exec into it.
CMD ["tail", "-f", "/dev/null"]
