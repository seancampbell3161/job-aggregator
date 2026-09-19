# Shared app image for the local runtime (poller daemon + web UI).
#
# Two targets: `runtime` (the default, slim, ~494 MB) and `headless`, which
# adds Playwright and Chromium (~2.17 GB total) for the Avature board tier.
# Both extend a common `base`, so `runtime` adds nothing on top of it and a
# registry client that already has one image pulls only the difference for
# the other. `runtime` is defined LAST on purpose: Docker builds the final
# stage when no `--target` is given, so a bare `docker build .` must land on
# the slim image, not the 2 GB browser one.
FROM python:3.12-slim AS base

# WeasyPrint native deps (Debian) for the tailor PDF render path.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 \
        libffi8 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# The app runs unprivileged; docker-entrypoint.sh hands off to this user.
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 --no-create-home app

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml README.md LICENSE /app/
COPY src/ /app/src/
RUN pip install --no-cache-dir "/app[web,render]"

# Enterprise-board discovery's seed CSV — load_seeds(DEFAULT_SEEDS) reads this
# at /app/scripts/seeds/enterprise_companies.csv every discovery cycle; without
# it the discovery tier's board sweep raises FileNotFoundError in prod.
COPY scripts/seeds/ /app/scripts/seeds/
# The settings UI reads per-flag help out of the configuration reference.
COPY docs/CONFIG.md /app/docs/CONFIG.md
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Personal settings, documents, and secrets live in the app database under
# /data; a fresh container boots into "not set up" until it is configured in
# the web UI or a backup is restored.
ENV JOB_AGG_SQLITE_PATH=/data/job_aggregator.db \
    JOB_AGG_TAILORED_DIR=/data/tailored \
    JOB_AGG_TEMPLATES_DIR=/data/templates \
    JOB_AGG_WEB_HOST=0.0.0.0
# Inside a container the network namespace is the isolation boundary; the -p
# publish flag on docker run is the security gate. Binding 127.0.0.1 makes the
# app reachable only from inside the container, not through -p. A host-side
# `python -m src.web` still defaults to 127.0.0.1 and remains safe.

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
# Default command is the web UI; compose overrides for the poller.
CMD ["python", "-m", "src.web"]

# The browser tier. Kept out of the default image because the only board
# family it serves (sources.avature) defaults to empty.
FROM base AS headless
# `playwright install` runs as root and would otherwise drop Chromium in
# /root/.cache/ms-playwright, which the unprivileged app user cannot read — the
# browser would then fail to launch at runtime with a path error, long after
# the build looked fine. Install to a shared location and make it world-readable.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir "/app[headless]" \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

# The default image: base, with no browser. LAST on purpose — Docker builds the
# final stage when no --target is given, so a bare `docker build .` must land
# here and not on the 2 GB browser image.
FROM base AS runtime
