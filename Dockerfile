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

# Résumé assets are copied so the image also runs standalone; config.yaml and
# profile.md are personal (untracked) and only bind-mounted at runtime — a bare
# `docker run` without mounts fails at startup with copy instructions.
COPY resume/ /app/resume/
# Enterprise-board discovery's seed CSV — load_seeds(DEFAULT_SEEDS) reads this
# at /app/scripts/seeds/enterprise_companies.csv every discovery cycle; without
# it the discovery tier's board sweep raises FileNotFoundError in prod.
COPY scripts/seeds/ /app/scripts/seeds/

ENV JOB_AGG_BACKEND=sqlite \
    JOB_AGG_SQLITE_PATH=/data/job_aggregator.db \
    JOB_AGG_TAILORED_DIR=/data/tailored

# Default command is the web UI; compose overrides for the poller.
CMD ["python", "-m", "src.web"]
