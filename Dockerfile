# Anthill — daemon-ready image.
#
# Two stages:
#   1. builder  — install with all opt-in extras so the runtime image
#                 has docs/daemon/browser deps prebuilt.
#   2. runtime  — slim python:3.11 base, copies the wheel + venv.
#
# Build:
#   docker build -t anthill-agent .
#
# Run (daemon serving Lark + DeepSeek):
#   docker run --rm -p 8765:8765 \
#     -e ANTHILL_DEEPSEEK_KEY=sk-... \
#     -e ANTHILL_LARK_APP_ID=cli_... \
#     -e ANTHILL_LARK_APP_SECRET=... \
#     -v anthill-state:/root/.anthill \
#     anthill-agent
#
# Run interactive REPL one-shot:
#   docker run --rm -it \
#     -e ANTHILL_DEEPSEEK_KEY=sk-... \
#     anthill-agent anthill

ARG PYTHON_VERSION=3.11

# ---------- builder ----------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src

# Install with daemon + docs extras. Browser left out by default
# (Chromium binaries are 200MB+ — opt in by extending this image).
RUN pip install --upgrade pip && \
    pip install '.[daemon,docs]'

# ---------- runtime ----------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ANTHILL_DAEMON_HOST=0.0.0.0 \
    ANTHILL_DAEMON_PORT=8765

RUN useradd --create-home --shell /bin/bash anthill
USER anthill
WORKDIR /home/anthill

COPY --from=builder /usr/local/lib/python*/site-packages /usr/local/lib/python*/site-packages
COPY --from=builder /usr/local/bin/anthill /usr/local/bin/anthill

EXPOSE 8765

# Persist nation state across container restarts.
VOLUME /home/anthill/.anthill

# Default to running the webhook daemon. Override with any other
# anthill subcommand (anthill ask, anthill repl, etc).
CMD ["anthill", "serve"]
