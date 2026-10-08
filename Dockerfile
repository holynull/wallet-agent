FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY skills ./skills
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev

ENV PYTHONPATH=/app/src
EXPOSE 8000
CMD [".venv/bin/uvicorn", "wallet_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
