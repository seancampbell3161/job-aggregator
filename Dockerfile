# Shared app image for the local runtime (poller daemon + web UI).
FROM python:3.12-slim

# WeasyPrint native deps (Debian) for the tailor PDF render path.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 \
        libffi8 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml README.md LICENSE /app/
COPY src/ /app/src/
RUN pip install --no-cache-dir "/app[web,render,headless]"
RUN python -m playwright install --with-deps chromium

# Résumé examples, fonts, and built-in assets ship in the image. Personal
# settings, documents, and secrets live in the app database under /data; a
# fresh container boots into "not set up" until `python -m src.settings import`.
COPY resume/ /app/resume/
# Enterprise-board discovery's seed CSV — load_seeds(DEFAULT_SEEDS) reads this
# at /app/scripts/seeds/enterprise_companies.csv every discovery cycle; without
# it the discovery tier's board sweep raises FileNotFoundError in prod.
COPY scripts/seeds/ /app/scripts/seeds/
# The settings UI reads per-flag help out of the configuration reference.
COPY docs/CONFIG.md /app/docs/CONFIG.md

ENV JOB_AGG_SQLITE_PATH=/data/job_aggregator.db \
    JOB_AGG_TAILORED_DIR=/data/tailored \
    JOB_AGG_TEMPLATES_DIR=/data/templates

# Default command is the web UI; compose overrides for the poller.
CMD ["python", "-m", "src.web"]
