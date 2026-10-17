# syntax=docker/dockerfile:1

# One image runs both processes. Compose chooses the command:
#   web:    python -m app web
#   worker: python -m app worker
# python:3.11-slim, resolved on 2026-10-16 to Python 3.11.17 on Debian trixie.
FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

# The LightGBM wheel links against the system OpenMP runtime, which slim images lack.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# The same uv version that wrote uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /srv

# Dependencies get their own layer, so a code change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY . .

# Run unprivileged. The code stays read-only. The generator writes data/ at run time.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /srv/data \
    && chown appuser:appuser /srv/data
USER appuser

ENV PATH="/srv/.venv/bin:$PATH"
EXPOSE 8000
CMD ["python", "-m", "app", "web"]
