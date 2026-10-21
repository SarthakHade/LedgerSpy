# LedgerSpy PUBLIC DEMO image (synthetic data only) for a free Render web service.
# Build from the repository root:  docker build -f deploy/demo/Dockerfile -t ledgerspy-demo .
# Stages: base (runtime contents), test (local verification only, never deployed), runtime (LAST: what Render runs).
# No secret is used at build time or stored in the image: every secret comes from the service's environment.

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Tesseract (English) for scanned PDF pages; tini as PID 1 (signals, and reaping orphaned worker children).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tini \
 && rm -rf /var/lib/apt/lists/*

# Runtime wheels: the release versions, hash-pinned for Linux (deploy/demo/make_linux_lock.py).
COPY deploy/demo/requirements-linux.lock /tmp/requirements-linux.lock
RUN pip install --require-hashes --no-deps --only-binary=:all: -r /tmp/requirements-linux.lock \
 && rm /tmp/requirements-linux.lock

# The application (root-owned, read-only for the service user) and only the synthetic reference data.
WORKDIR /app
COPY pyproject.toml ./
COPY config/default.yaml ./config/default.yaml
COPY ledgerspy ./ledgerspy
COPY ledgerspy_demo ./ledgerspy_demo
COPY frontend ./frontend
COPY data/synthetic/reference ./data/synthetic/reference
ENV PYTHONPATH=/app

# An unprivileged service user; its only writable place is the (ephemeral) demo data folder.
RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin ledgerspy \
 && install -d -o ledgerspy -g ledgerspy -m 0700 /var/lib/ledgerspy-demo \
 && install -d -o ledgerspy -g ledgerspy -m 0700 /opt/seed-build

# The synthetic seed, built once here (the 0.1-CPU host does not import it at every start). It is built as the
# service user (evidence verification checks that files are not writable, which is never true for root), then
# handed to root: at run time the service verifies it and can only read it.
USER ledgerspy
RUN python -m ledgerspy_demo.prebuild /opt/seed-build/seed
USER root
RUN mv /opt/seed-build/seed /app/seed && rmdir /opt/seed-build && chown -R root:root /app/seed

ENV LEDGERSPY_MODE=demo \
    LEDGERSPY_DEMO_DATA_DIR=/var/lib/ledgerspy-demo/data \
    LEDGERSPY_DEMO_SEED_DIR=/app/seed \
    LEDGERSPY_DEMO_BIND_HOST=0.0.0.0


# ------------------------------------------------------------------------- test (local verification only)
FROM base AS test
RUN pip install pytest==9.1.1 pytest-socket==0.8.1 httpx2==2.13.1
COPY tests ./tests
RUN chown -R ledgerspy:ledgerspy /app/tests
USER ledgerspy
# tini as PID 1, as in the runtime stage: killed worker trees are reaped (no zombies kept as "running")
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "pytest", "tests/demo", "-q", "-p", "no:cacheprovider", "-rs"]


# ------------------------------------------------------------------------- runtime (deployed)
FROM base AS runtime
USER ledgerspy
EXPOSE 10000
# The platform sets PORT; the health check answers without the public Host (a fixed "ok", no data).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '10000'), timeout=4)"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "ledgerspy_demo"]
