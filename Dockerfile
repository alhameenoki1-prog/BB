FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first (cached layer — rebuilds only when requirements change),
# then Chromium + its system libraries in the same layer cleanup pass.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# Non-root user; owns app data and the shared browser install
RUN useradd --create-home --uid 1000 harvey \
    && mkdir -p /app/data \
    && chown -R harvey:harvey /app /ms-playwright

# Copy project (prompts/skills can be bind-mounted over these at runtime)
COPY --chown=harvey:harvey harvey/ harvey/
COPY --chown=harvey:harvey bb/ bb/
COPY --chown=harvey:harvey prompts/ prompts/
COPY --chown=harvey:harvey skills/ skills/
COPY --chown=harvey:harvey harvey.yaml .

USER harvey
ENV PATH="/home/harvey/.local/bin:${PATH}"

# The brain calls the Anthropic API directly: pass ANTHROPIC_API_KEY through
# the environment (docker-compose reads it from .env). No CLI to install.

# Default: the hosted multi-user web app (bb/). It keeps every user's state
# under BB_HOME (/app/data/bb) and starts one Harvey worker per user inside
# this container. To run a single-user Harvey instead:
#   docker compose run harvey python -m harvey.main
ENV BB_HOME=/app/data/bb BB_HOST=0.0.0.0 BB_PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=1m --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status == 200 else 1)"

CMD ["python", "-m", "bb"]
