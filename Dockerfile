# Pitch Lab on Cloud Run. The database is not in the image: ensure_db() downloads
# gs://mlb-agent-2026/baseball-agent/v3/baseball.duckdb (164 MB) at startup.
FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
ENV HOST=0.0.0.0 PORT=8080
CMD ["uv", "run", "--no-dev", "app.py"]
