FROM python:3.11-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]" 2>/dev/null || pip install --no-cache-dir .

# Copy application code
COPY src/ src/
COPY scripts/ scripts/
COPY config/ config/

# Create non-root user
RUN useradd --create-home botuser
USER botuser

# Default command
ENTRYPOINT ["python", "scripts/run.py"]
CMD ["--config", "config/settings.yaml"]
