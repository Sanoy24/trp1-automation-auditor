# Use a modern Python base image
FROM python:3.13-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set working directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml .
COPY .python-version .

# Install dependencies using uv
# --frozen ensures we use the exact lockfile versions
RUN uv sync --frozen

# Copy source code and rubric
COPY src/ ./src/
COPY rubric/ ./rubric/
COPY main.py .
COPY README.md .

# Create output directories
RUN mkdir -p audit/report_onself_generated audit/report_onpeer_generated reports

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

# Default command: show help
ENTRYPOINT ["uv", "run", "python", "main.py"]
CMD ["--help"]
