FROM python:3.12-slim

# Create a non-root user so the bot does not run as root in the container.
RUN groupadd --system mombot && useradd --system --gid mombot mombot

WORKDIR /app

# Copy only the files needed for installation first (layer-cache friendly).
# Alembic migrations and alembic.ini are included so the image can serve
# as the Container Apps Job entrypoint for 'alembic upgrade head' (issue #255).
# migrate.sh is the ENTRYPOINT override used by the migrations job.
COPY pyproject.toml ./
COPY uv.lock ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY migrate.sh ./
RUN chmod +x ./migrate.sh

# Install the package and its runtime deps from the locked lockfile.
# --no-dev omits test/lint tooling from the image.
RUN pip install uv --no-cache-dir && uv sync --frozen --no-dev

# Switch to the non-root user for runtime.
USER mombot

# `python -m mom_bot` invokes src/mom_bot/__main__.py.
# The real Discord client will be wired in Epic 0.3 (issue #13).
# Invoke the venv's python directly — `uv run` requires a writable
# $HOME/.cache/uv that the --system mombot user does not have (#118).
CMD ["/app/.venv/bin/python", "-m", "mom_bot"]
