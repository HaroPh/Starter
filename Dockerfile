# No `# syntax=` directive on purpose: it would pull docker.io/docker/dockerfile, which is
# outside the image allowlist in docs/image-policy.md.
#
# Architecture neutrality: no --platform, no TARGETARCH branching, nothing compiled from
# source. Both base images are multi-arch manifests and psycopg ships manylinux wheels for
# x86_64 and aarch64, so the same Dockerfile produces a working image on amd64 and arm64.

FROM python:3.13.15-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# ---------------------------------------------------------------------------
FROM base AS builder
# Copy the uv binary into the same python base used at runtime, rather than building on a
# uv-flavoured image. One base image means zero Python or distro skew between build and run.
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
WORKDIR /app

# Dependencies first, so editing application code does not invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app

# ---------------------------------------------------------------------------
# Test/eval target: same tree, plus dev dependencies. Kept out of the runtime image.
FROM builder AS test
RUN uv sync --frozen
COPY tests ./tests
COPY evals ./evals

# ---------------------------------------------------------------------------
FROM base AS runtime
RUN groupadd -g 10001 app \
 && useradd -u 10001 -g app -m -s /usr/sbin/nologin app
WORKDIR /app
COPY --from=builder --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH"
USER app
EXPOSE 3000

# Uses the Python standard library rather than curl: slim images have no curl, and adding
# it would mean an apt layer for a job stdlib already does.
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=12 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3000/healthz', timeout=2).status == 200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000"]
